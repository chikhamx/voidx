from __future__ import annotations

from langchain_core.messages import ToolMessage

from voidx.agent.graph.runtime_guards import (
    GuardDecision,
    GuardGuidance,
    RuntimeGuardState,
    build_failure_key,
    cycle_summary_from_tools,
)
from voidx.tools.service import ToolResult

from .types import _ExecutedTool, ToolResultOk


def _runtime_guard_state(host) -> RuntimeGuardState:
    state = getattr(host, "_runtime_guards", None)
    if state is None:
        state = RuntimeGuardState()
        host._runtime_guards = state
    return state


def _split_runtime_guard_blocked_calls(
    tool_calls: list[dict],
    guard_state: RuntimeGuardState,
) -> tuple[list[dict], dict[int, _ExecutedTool]]:
    runnable: list[dict] = []
    blocked: dict[int, _ExecutedTool] = {}
    for call in tool_calls:
        if guard_state.tool_failures.should_block(call):
            blocked[id(call)] = _runtime_guard_blocked_tool(call)
        else:
            runnable.append(call)
    return runnable, blocked


def _runtime_guard_blocked_tool(tool_call: dict) -> _ExecutedTool:
    tool_name = str(tool_call.get("name") or "tool")
    message = (
        f"Runtime guard blocked repeated failed tool call: {tool_name}. "
        "Stop retrying it with the same arguments; summarize the failure, use a different approach, "
        "or ask the user for the missing input."
    )
    result = ToolResult(
        title="runtime guard: repeated failure",
        output=message,
        summary="runtime guard blocked repeated failed tool call",
        metadata={
            "blocked": True,
            "runtime_guard": "tool_failure_loop",
            "error_kind": "runtime_guard_blocked",
        },
    )
    return _ExecutedTool(
        message=ToolMessage(content=message, tool_call_id=tool_call.get("id", ""), status="error"),
        result=result,
        tool_call=tool_call,
    )


def _restore_runtime_guard_blocked_results(
    original_calls: list[dict],
    runnable_executed: list[_ExecutedTool],
    blocked: dict[int, _ExecutedTool],
) -> list[_ExecutedTool]:
    if not blocked:
        return runnable_executed
    restored: list[_ExecutedTool] = []
    runnable_iter = iter(runnable_executed)
    for call in original_calls:
        item = blocked.get(id(call))
        if item is not None:
            restored.append(item)
        else:
            restored.append(next(runnable_iter))
    return restored


def _runtime_guard_tool_messages(
    tool_calls: list[dict],
    message: str,
    *,
    metadata: dict | None = None,
) -> list[ToolMessage]:
    return [
        ToolMessage(content=message, tool_call_id=call.get("id", ""), status="error")
        for call in tool_calls
    ]


async def _record_runtime_guard_outcomes(
    host,
    guard_state: RuntimeGuardState,
    executed: list[_ExecutedTool],
    *,
    previous_todo_state,
    next_todo_state,
    workflow_changed: bool,
    result_ok: ToolResultOk,
) -> GuardDecision:
    guard_eligible = [item for item in executed if item.runtime_guard_eligible]
    for item in guard_eligible:
        metadata = getattr(item.result, "metadata", {}) or {}
        if metadata.get("runtime_guard"):
            continue
        if result_ok(item.result):
            guard_state.tool_failures.record_success(item.tool_call)
            continue
        key = build_failure_key(item.tool_call, item.result)
        guidance = guard_state.tool_failures.record_failure(
            key,
            str(getattr(item.result, "summary", "") or getattr(item.result, "output", ""))[:500],
        )
        _submit_guard_guidance(host, guidance)

    summary = cycle_summary_from_tools(
        guard_eligible,
        previous_todo_state=previous_todo_state,
        next_todo_state=next_todo_state,
        workflow_changed=workflow_changed,
        result_ok=result_ok,
    )
    if summary.tool_names:
        guidance = guard_state.repetitive_tools.record_cycle(summary)
        _submit_guard_guidance(host, guidance)
        guidance = guard_state.no_progress.record_cycle(summary)
        _submit_guard_guidance(host, guidance)
        no_progress_decision = guard_state.no_progress.decision()
        if no_progress_decision.action == "terminate":
            return no_progress_decision
    return GuardDecision()


def _latest_action_from_summary(summary) -> str:
    if summary.only_tool:
        return summary.only_tool
    return ", ".join(summary.tool_names[:3])


def _submit_guard_guidance(host, guidance: GuardGuidance | None) -> None:
    if guidance is None:
        return
    submit = getattr(host, "submit_guidance", None)
    if callable(submit):
        submit(guidance.message, source="guard")
        return
    pending = getattr(host, "_pending_guidance", None)
    if isinstance(pending, list):
        pending.append((guidance.message, False, "guard"))
