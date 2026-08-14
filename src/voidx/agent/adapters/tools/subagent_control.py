"""Control existing child-agent runs."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator

from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext
from voidx.agent.domain.subagent import AgentGatewayError, AgentRun
from voidx.agent.domain.subagent_display import subagent_display_name
from voidx.agent.application.subagent_status import (
    activity_label,
    activity_recommendation,
    format_duration,
    public_child_run_snapshot,
    render_child_activity,
    render_child_elapsed,
    render_child_progress,
)
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.schema import model_to_json_schema


_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_WAIT_TIMEOUT = 256.0
_CONTROL_ERROR_HINTS = {
    "unknown_run": "Verify the run IDs and parent-child control relationship before retrying.",
    "route_not_allowed": "Verify the run IDs and parent-child control relationship before retrying.",
    "cross_session": "Verify the run IDs and parent-child control relationship before retrying.",
    "cancel_timeout": "Cancellation was not acknowledged; do not retry automatically, and report that the run may still be active.",
    "gateway_error": "Inspect the control error before retrying.",
}
_CHILD_FAILURE_HINT = "Inspect the error and start a replacement run if the task is still needed."
_INCOMPLETE_HINT = "Use the partial result if sufficient; otherwise start a narrower replacement task."


class AgentControlInput(BaseModel):
    action: Literal["wait", "cancel"]
    run_id: str | list[str]

    @field_validator("run_id", mode="before")
    @classmethod
    def _normalize_run_ids(cls, value):
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not values:
            raise ValueError("run_id must be a non-empty string or list of strings")
        normalized: list[str] = []
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("every run_id must be a non-empty string")
            run_id = item.strip()
            if run_id not in normalized:
                normalized.append(run_id)
        return normalized


class AgentControlTool:
    id = "agent_control"
    description = (
        "Wait for or cancel one or more existing child-agent runs. "
        "Wait is finite (up to 256 seconds per call)."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(AgentControlInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = AgentControlInput.model_validate(args)
        except ValidationError as exc:
            return ToolResult(
                output=f"Agent control rejected: {exc.errors()[0].get('msg', 'invalid arguments')}",
                metadata={"error": True, "validation_error": True},
                next_step_hint="Correct the arguments before retrying.",
            )
        if ctx.runtime.subagent_transport is None or not ctx.runtime.run_id:
            return ToolResult(
                output="Agent gateway is unavailable for agent_control.",
                metadata={"error": True, "reason": "gateway_unavailable"},
                next_step_hint="Restore agent gateway availability before retrying.",
            )

        run_ids = list(inp.run_id)
        items = await asyncio.gather(*(
            self._execute_one(inp.action, run_id, ctx)
            for run_id in run_ids
        ))
        if len(items) == 1:
            return _single_result(inp.action, items[0])
        return _batch_result(inp.action, items)

    async def _execute_one(
        self,
        action: str,
        run_id: str,
        ctx: ToolContext,
    ) -> dict:
        wait_started_at = time.time() if action == "wait" else None
        try:
            if action == "wait":
                run = await ctx.runtime.subagent_transport.wait(
                    requester_run_id=ctx.runtime.run_id,
                    target_run_id=run_id,
                    timeout=_WAIT_TIMEOUT,
                )
            else:
                run = await ctx.runtime.subagent_transport.cancel(
                    requester_run_id=ctx.runtime.run_id,
                    target_run_id=run_id,
                )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            reason = exc.reason if isinstance(exc, AgentGatewayError) else "gateway_error"
            return {
                "run_id": run_id,
                "status": "error",
                "error": True,
                "reason": reason,
                "detail": detail[:200],
            }
        return _success_item(
            action,
            run,
            wait_timeout_seconds=_WAIT_TIMEOUT if action == "wait" else None,
            wait_started_at=wait_started_at,
            sampled_at=time.time() if action == "wait" else None,
        )


def _success_item(
    action: str,
    run,
    *,
    wait_timeout_seconds: float | None = None,
    wait_started_at: float | None = None,
    sampled_at: float | None = None,
) -> dict:
    item = {
        "run_id": run.run_id,
        "run": public_child_run_snapshot(run),
        "status": run.status,
    }
    if action == "wait":
        terminal = run.status in _TERMINAL_STATUSES
        wait_outcome = run.wait_outcome or (
            "already_terminal" if terminal else "timed_out"
        )
        item.update({
            "wait_outcome": wait_outcome,
            "result_quality": _result_quality(run),
            "finish_reason": _finish_reason(run),
        })
        if wait_outcome == "timed_out":
            item.update({
                "wait_timeout_seconds": wait_timeout_seconds,
                "_wait_started_at": wait_started_at,
                "_sampled_at": sampled_at,
            })
    return item


def _single_result(action: str, item: dict) -> ToolResult:
    output = _render_item(item)
    name = subagent_display_name(item["run_id"])
    if item["status"] == "error":
        metadata = dict(item)
        metadata.pop("status")
        return ToolResult(
            output=output,
            display=f"{name} error.",
            summary=f"{name} error",
            metadata=metadata,
            next_step_hint=_hints(action, [item]),
        )
    metadata = {
        key: value for key, value in item.items()
        if key != "run_id" and not key.startswith("_")
    }
    return ToolResult(
        output=output,
        display=f"{name} {item['status']}.",
        summary=f"{name} {item['status']}",
        metadata=metadata,
        next_step_hint=_hints(action, [item]),
    )


def _batch_result(action: str, items: list[dict]) -> ToolResult:
    counts: dict[str, int] = {}
    for item in items:
        status = item["status"]
        counts[status] = counts.get(status, 0) + 1
    error_count = counts.get("error", 0)
    public_items = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in items
    ]
    metadata: dict = {"action": action, "items": public_items, "counts": counts}
    if error_count == len(items):
        metadata["error"] = True
    elif error_count:
        metadata["partial_error"] = True
    summary = ", ".join(f"{count} {status}" for status, count in counts.items())
    return ToolResult(
        output="\n\n".join(_render_item(item) for item in items),
        display=f"{len(items)} agents",
        summary=summary,
        metadata=metadata,
        next_step_hint=_hints(action, items),
    )


def _render_item(item: dict) -> str:
    name = subagent_display_name(item["run_id"])
    status = item["status"]
    if status == "error":
        return f"{name} [error]\nError: {item['detail']}"
    run = item["run"]
    finish_reason = item.get("finish_reason") or ""
    suffix = f"; finish_reason={finish_reason}" if finish_reason else ""
    lines = [f"{name} [{status}{suffix}]"]
    result_text = _result_output(run.get("result"))
    if status == "failed":
        error = str(run.get("error") or result_text).strip()
        if error:
            lines.append(f"Error: {error}")
    elif result_text:
        lines.extend(["Result:", result_text])
    if status == "running" and item.get("wait_outcome") == "timed_out":
        timeout_seconds = float(item.get("wait_timeout_seconds") or _WAIT_TIMEOUT)
        sampled_at = float(item.get("_sampled_at") or time.time())
        agent_run = AgentRun.model_validate(run)
        lines.append(f"Wait timed out after {timeout_seconds:g}s.")
        lines.append(f"Status: {render_child_elapsed(agent_run, sampled_at=sampled_at)}")
        progress = render_child_progress(agent_run.progress)
        if progress:
            lines.append(f"Progress: {progress}")
        lines.extend(render_child_activity(agent_run, sampled_at=sampled_at))
    return "\n".join(lines)


def _hints(action: str, items: list[dict]) -> str:
    hints: list[str] = []
    for item in (value for value in items if value.get("wait_outcome") == "timed_out"):
        hints.append(_timeout_hint(item, include_name=len(items) > 1))
    for item in items:
        if item["status"] == "error":
            _append_unique(hints, _CONTROL_ERROR_HINTS.get(item.get("reason"), _CONTROL_ERROR_HINTS["gateway_error"]))
    if action == "wait" and any(item["status"] == "failed" for item in items):
        _append_unique(hints, _CHILD_FAILURE_HINT)
    if action == "wait" and any(item.get("finish_reason") for item in items):
        _append_unique(hints, _INCOMPLETE_HINT)
    return "\n".join(hints)


def _timeout_hint(item: dict, *, include_name: bool) -> str:
    run = AgentRun.model_validate(item["run"])
    sampled_at = float(item.get("_sampled_at") or time.time())
    wait_started_at = float(item.get("_wait_started_at") or sampled_at)
    timeout_seconds = float(item.get("wait_timeout_seconds") or _WAIT_TIMEOUT)
    current = activity_label(run.current_activity) if run.current_activity is not None else "other"
    age = format_duration(max(0.0, sampled_at - (run.last_activity_at or sampled_at)))
    if activity_recommendation(run, wait_started_at=wait_started_at) == "wait":
        text = f"Activity was observed {age} ago; current state is {current}. Wait again if the result is still needed."
    else:
        text = (
            f"No activity was observed during the {timeout_seconds:g}s wait; current state is {current} "
            f"and its last activity was {age} ago. Cancel the child agent unless this duration is expected."
        )
    if not include_name:
        return text
    return f"{subagent_display_name(item['run_id'])}: {text[0].lower()}{text[1:]}"


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _finish_reason(run) -> str:
    if not isinstance(run.result, dict):
        return ""
    reason = str(run.result.get("finish_reason") or "").strip()
    return reason if reason not in {"", "final_answer", "message_result"} else ""


def _result_quality(run) -> str:
    if run.status != "completed":
        return "not_available" if not run.result else "terminal_error_result"
    if _finish_reason(run) == "contract_unsatisfied":
        return "incomplete_contract"
    if _finish_reason(run):
        return "incomplete_execution"
    return "available"


def _result_output(result: dict | None) -> str:
    if not result:
        return ""
    for key in ("result", "output", "content", "text"):
        if key in result:
            return str(result.get(key) or "")
    return json.dumps(result, ensure_ascii=False, default=str)


__all__ = ["AgentControlInput", "AgentControlTool", "_WAIT_TIMEOUT"]
