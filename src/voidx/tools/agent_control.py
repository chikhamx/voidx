"""Control an existing child-agent run."""

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema
from voidx.ui.output.agent_display import subagent_display_name


_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_WAIT_TIMEOUTS = {"brief": 5.0, "extended": 30.0, "until_complete": 0.0}


class AgentControlInput(BaseModel):
    action: Literal["wait", "cancel"]
    run_id: str
    wait: Literal["brief", "extended", "until_complete"] = Field(
        default="until_complete",
        description="Wait strategy; ignored for cancel.",
    )


class AgentControlTool(BaseTool):
    id = "agent_control"
    description = (
        "Wait for or cancel an existing child-agent run. "
        "A terminal wait result must not be polled again."
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
            )
        if ctx.agent_gateway is None or not ctx.agent_run_id:
            return ToolResult(
                output="Agent gateway is unavailable for agent_control.",
                metadata={"error": True, "reason": "gateway_unavailable"},
            )
        try:
            if inp.action == "wait":
                run = await ctx.agent_gateway.wait(
                    requester_run_id=ctx.agent_run_id,
                    target_run_id=inp.run_id,
                    timeout=_WAIT_TIMEOUTS[inp.wait],
                )
            else:
                run = await ctx.agent_gateway.cancel(
                    requester_run_id=ctx.agent_run_id,
                    target_run_id=inp.run_id,
                )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            return ToolResult(
                output=f"agent_control({inp.action}) failed: {detail}",
                metadata={"error": True, "reason": "gateway_error", "detail": detail[:200]},
            )
        display_name = subagent_display_name(run.run_id)
        if inp.action == "wait":
            return _wait_result(run, display_name)
        return ToolResult(
            output=_result_output(run.result) or run.error or run.status,
            display=f"{display_name} {run.status}.",
            summary=f"{display_name} {run.status}",
            metadata={"run": run.model_dump(mode="json"), "status": run.status},
        )


def _wait_result(run, display_name: str) -> ToolResult:
    status = run.status
    terminal = status in _TERMINAL_STATUSES
    outcome = run.wait_outcome or (
        "already_terminal" if terminal else "timed_out_still_running"
    )
    result_quality = _result_quality(run)
    finish_reason = _finish_reason(run)
    result_text = _result_output(run.result) or run.error or "No final result is available yet."
    lines = [
        f"Agent run status: {status}",
        f"Wait outcome: {outcome}",
        f"Terminal: {str(terminal).lower()}",
        f"Result quality: {result_quality}",
    ]
    if finish_reason:
        lines.append(f"Finish reason: {finish_reason}")
    lines.extend(["", "Final result:", result_text])
    if terminal:
        if outcome == "already_terminal":
            lines.extend([
                "",
                "This wait returned the cached terminal result; no new work was performed.",
            ])
        if result_quality == "incomplete_contract":
            lines.extend([
                "",
                "The child run is terminal and cannot produce a new result by waiting.",
            ])
        lines.extend([
            "",
            "Do not call agent_control(wait) again for this run.",
        ])
        next_step_hint = "This run is terminal; do not call agent_control(wait) again."
    else:
        lines.extend([
            "",
            "The wait window expired while the child was still running.",
            "Do not call wait repeatedly in a tight loop; take another concrete action or check again only when necessary.",
        ])
        next_step_hint = f"Run {run.run_id} is still active; do not poll in a tight loop."
    return ToolResult(
        output="\n".join(lines),
        display=f"{display_name} {status}.",
        summary=f"{display_name} {status} ({outcome})",
        metadata={
            "run": run.model_dump(mode="json"),
            "status": status,
            "wait_outcome": outcome,
            "terminal": terminal,
            "result_quality": result_quality,
            "finish_reason": finish_reason,
        },
        next_step_hint=next_step_hint,
    )


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
    return ""


__all__ = ["AgentControlInput", "AgentControlTool", "_WAIT_TIMEOUTS"]
