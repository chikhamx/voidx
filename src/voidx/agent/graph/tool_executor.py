"""Composition component for the agent graph tool execution node."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from voidx.diffing import diff_stat
from voidx.agent.graph.runtime import current_parent_tool_call_id
from voidx.agent.graph.runtime_guards import (
    GuardDecision,
    GuardGuidance,
    RuntimeGuardState,
    build_failure_key,
    cycle_summary_from_tools,
)
from voidx.agent.graph.todo_events import todo_updated_event
from voidx.agent.todo_state import apply_todo_state_to_host, todo_run_state_from_result
from voidx.agent.task_state import GoalSpec, TaskState, TodoRunState, ToolStatePatch, goal_label
from voidx.runtime.intent import TaskIntent
from voidx.agent.tool_messages import sanitize_tool_message_content
from voidx.workflow.service import advance_workflow_states, auto_advance_events, is_workflow_terminal_condition
from voidx.workflow.route import (
    workflow_path_reaches,
    workflow_route_end,
    workflow_route_start,
    workflow_transition_target,
)
from voidx.workflow.types import (
    WorkflowEvidence,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
)
from voidx.tools.service import ToolContext, ToolResult, UserInteraction, UserResponse
from voidx.agent.tool_result_storage import maybe_persist_tool_result
from voidx.runtime.ui import (
    DEFAULT_DISPLAY_RULES,
    FileChangeAppended,
    StatusFinished,
    StatusUpdated,
    ToolDisplayMode,
    ToolDisplayPolicy,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    WarningAppended,
    _fmt_args,
    _title,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphToolExecutionHost


def _invalidate_tui(host: object) -> None:
    app = getattr(host, "_app", None)
    if app is not None and callable(getattr(app, "invalidate", None)):
        app.invalidate()


_OTHER_VALUE_PREFIX = "__voidx_choice_prompt_other__"
AGENT_RESULT_PREVIEW_LINES = 5
AGENT_RESULT_PREVIEW_CHARS = 1200


@dataclass
class _ExecutedTool:
    message: ToolMessage | None
    result: object
    tool_call: dict
    todo_state: TodoRunState | None = None


ToolResultOk = Callable[[object], bool]


class GraphToolExecutor:
    """Executes approved tool calls for a graph host."""

    def __init__(self, host: GraphToolExecutionHost) -> None:
        self.host = host

    async def execute_tools(
        self,
        state,
        *,
        tool_result_ok: ToolResultOk | None = None,
    ) -> dict:
        host = self.host
        result_ok = tool_result_ok or self.tool_result_ok
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {}

        if host._ui.dock.active and host._ui.dock.current_agent is not None:
            host._turn_node = host._ui.dock.current_agent

        host._current_messages = state["messages"]
        runtime_persona = state.get("persona", "coordinate")
        session_id = host._session.id if host._session else "default"
        plan_mode = state.get("plan_mode", False)
        interaction_mode = state.get("interaction_mode")
        workspace = state.get("workspace", host._workspace)
        runtime_task_state = _task_state_for_state(
            state.get("task_state"),
            fallback=getattr(host, "_task_state", None),
        )
        runtime_task_intent = runtime_task_state.current_intent.value
        runtime_goal = runtime_task_state.current_goal
        runtime_workflow_runs = list((runtime_task_state.workflow_runs or {}).values())
        turn_count = int(state.get("step_count", 0) or 0)
        state_update: dict = {}
        display_policy = getattr(host, "_display_policy", None) or ToolDisplayPolicy(rules=DEFAULT_DISPLAY_RULES)

        def make_context() -> ToolContext:
            return ToolContext(
                workspace=workspace,
                session_id=session_id,
                persona=runtime_persona,
                interaction_mode=interaction_mode or ("plan" if plan_mode else "auto"),
                task_intent=str(runtime_task_intent or "coding"),
                goal_type=runtime_goal.type.value if runtime_goal is not None else "",
                goal_target=goal_label(runtime_goal),
                active_workflow_names=_active_workflow_names(runtime_workflow_runs),
                workflow_runs=runtime_workflow_runs,
                workflow_route=runtime_task_state.workflow_route.model_dump(mode="json")
                if runtime_task_state.workflow_route is not None
                else None,
                file_mtimes=host._file_mtimes,
                mcp_manager=getattr(host, "_mcp_manager", None),
                lsp_manager=getattr(host, "_lsp_manager", None),
                sandbox_mode=host._permission.sandbox_mode,
                sandbox_extra_paths=host._permission.sandbox_workspace_write,
                interact=_make_interact_callback(getattr(host, "_app", None)),
            )

        ctx = make_context()

        def apply_state_update(update: dict) -> None:
            nonlocal ctx, runtime_goal, runtime_workflow_runs, runtime_task_intent, runtime_task_state, runtime_persona
            if not update:
                return
            if "persona" in update:
                runtime_persona = update.get("persona") or runtime_persona
                state_update["persona"] = runtime_persona
            if "should_continue" in update:
                state_update["should_continue"] = bool(update.get("should_continue"))
            if "task_state" in update:
                runtime_task_state = _task_state_for_state(update.get("task_state"))
                runtime_goal = runtime_task_state.current_goal
                state_update["task_state"] = runtime_task_state.model_dump(mode="json")
            if "todo_state" in update:
                apply_todo_state_to_host(host, update.get("todo_state"))
                runtime_task_state.todo_state = _todo_state_for_state(update.get("todo_state"))
                state_update["todo_state"] = update.get("todo_state")
                state_update["task_state"] = runtime_task_state.model_dump(mode="json")
            if "task_intent" in update:
                runtime_task_intent = update.get("task_intent") or "coding"
                runtime_task_state.current_intent = TaskIntent(runtime_task_intent)
                state_update["task_state"] = runtime_task_state.model_dump(mode="json")
            if "current_goal" in update:
                raw_goal = update.get("current_goal")
                runtime_goal = _goal_for_state(raw_goal)
                runtime_task_state.current_goal = runtime_goal
                state_update["task_state"] = runtime_task_state.model_dump(mode="json")
            if "workflow_runs" in update:
                runtime_workflow_runs = _workflow_runs_for_state(update.get("workflow_runs") or [])
                runtime_task_state.workflow_runs = {run.name: run for run in runtime_workflow_runs}
                state_update["task_state"] = runtime_task_state.model_dump(mode="json")
            # Sync runtime task state to host so status bar updates immediately
            if "task_state" in state_update:
                host._task_state = runtime_task_state.model_copy(deep=True)
                _invalidate_tui(host)
            ctx = make_context()

        tool_calls = last.tool_calls
        guard_state = _runtime_guard_state(host)
        repetitive_decision = guard_state.repetitive_tools.decision_for_pending(list(tool_calls))
        if repetitive_decision.action in {"skip", "terminate"}:
            tool_messages = _runtime_guard_tool_messages(
                tool_calls,
                repetitive_decision.message,
                metadata=repetitive_decision.metadata,
            )
            result: dict = {"messages": tool_messages}
            if repetitive_decision.action == "terminate":
                result["messages"] = [
                    *tool_messages,
                    AIMessage(content=repetitive_decision.message),
                ]
                result["should_continue"] = False
            return result

        # ── Phase 2: parallel execution of all approved tools ────────

        async def execute_one(tc):
            tid = tc["name"]
            targs = tc.get("args", {})
            cid = tc.get("id", "")

            tool_event_id = cid or f"{tid}:{id(tc)}"
            tool_node = None
            rule = display_policy.rule_for(tid)
            # ToolStarted uses the static rule mode (pre-execution); ToolResultAppended
            # may resolve to SUMMARY via auto-summary downgrade after output is available.
            initial_display_mode = rule.mode
            initial_summary_max_lines = rule.summary_max_lines
            if host._ui.via_events():
                gerund = _title(host._ui.ui._TOOL_GERUND.get(tid, tid + "ing"))
                tool_node = await host._ui.events.request(ToolStarted(
                    tool_call_id=tool_event_id,
                    tool_name=tid,
                    label=gerund,
                    args=_fmt_args(targs),
                    raw_args=targs,
                    display_mode=initial_display_mode,
                    summary_max_lines=initial_summary_max_lines,
                ))
                if host._ui.dock.active and host._ui.dock.current_agent is not None:
                    host._turn_node = host._ui.dock.current_agent
            elif host._ui.dock.active:
                if initial_display_mode != ToolDisplayMode.HIDDEN:
                    gerund = _title(host._ui.ui._TOOL_GERUND.get(tid, tid + "ing"))
                    tool_node = host._ui.dock.start_tool(
                        gerund,
                        _fmt_args(targs),
                        tool_call_id=tool_event_id,
                        tool_name=tid,
                        raw_args=targs,
                    )
                    if host._ui.dock.current_agent is not None:
                        host._turn_node = host._ui.dock.current_agent
            else:
                if initial_display_mode != ToolDisplayMode.HIDDEN:
                    host._ui.ui.tool_call(tid, targs)

            t0 = time.monotonic()
            ok = True
            try:
                host._ui.session_tracker.capture_tool_call(tid, targs, ctx.workspace, ctx.sandbox_extra_paths)
                parent_tool_token = current_parent_tool_call_id.set(tool_event_id)
                try:
                    result = await host.tools.execute_tool(tid, targs, ctx)
                finally:
                    current_parent_tool_call_id.reset(parent_tool_token)
                ok = result_ok(result)
            except Exception as e:
                from voidx.tools.service import ToolResult
                error_text = sanitize_tool_message_content(
                    f"Tool execution error: {e}",
                    workspace=ctx.workspace,
                )
                result = ToolResult(
                    output=error_text,
                    metadata={"error": sanitize_tool_message_content(str(e), workspace=ctx.workspace)},
                )
                ok = False
            elapsed = time.monotonic() - t0

            # on-failure: notify user when auto-approved tool fails
            if not ok:
                failure_tc = host._needs_failure_check.get(cid, None)
                if failure_tc and host._permission.approval_policy == "on-failure":
                    host._notify_tool_failure(failure_tc, result)
            elif ok:
                host._clear_failure_check(cid)

            todo_state = todo_run_state_from_result(result) if tid == "todo" and ok else None
            if host._ui.via_events() and tid == "todo":
                todo_event = todo_updated_event(result)
                if todo_event is not None:
                    await host._ui.events.emit(todo_event)
                elif ok:
                    await host._ui.events.emit(WarningAppended(
                        message="Todo update ignored: tool returned malformed metadata.",
                    ))
            elif tid == "todo" and ok and todo_state is None:
                host._ui.ui.warn("Todo update ignored: tool returned malformed metadata.")

            if host._ui.via_events():
                if not ok and initial_display_mode == ToolDisplayMode.HIDDEN:
                    await host._ui.events.emit(WarningAppended(
                        message=f"{tid} failed: {result.summary or 'unknown error'}",
                    ))
                else:
                    await host._ui.events.emit(ToolFinished(
                        tool_call_id=tool_event_id,
                        label=_title(tid),
                        elapsed=elapsed,
                        ok=ok,
                        detail=result.summary if result.summary else "",
                    ))
            elif tool_node:
                if not ok and initial_display_mode == ToolDisplayMode.HIDDEN:
                    host._ui.ui.warn(f"{tid} failed: {result.summary or 'unknown error'}")
                else:
                    host._ui.dock.finish_tool_node(tool_node, _title(tid), elapsed, ok)
            else:
                if not ok and initial_display_mode == ToolDisplayMode.HIDDEN:
                    host._ui.ui.warn(f"{tid} failed: {result.summary or 'unknown error'}")
                else:
                    host._ui.ui.tool_done(tid, elapsed, ok)

            # Render diff to terminal (if any)
            if getattr(result, "diff", None) and ok:
                host._ui.session_tracker.record_diff(result.diff)
                if host._ui.via_events():
                    await host._ui.events.emit(FileChangeAppended(
                        tool_call_id=tool_event_id,
                        diff_text=result.diff,
                    ))
                elif tool_node:
                    host._ui.dock.append_file_change(
                        result.diff,
                        parent=tool_node,
                        tool_call_id=tool_event_id,
                    )
                else:
                    added, removed = diff_stat(result.diff)
                    host._ui.ui.print(f"  [green]+{added}[/green] [red]−{removed}[/red]")
                if host._debug and not tool_node:
                    host._ui.ui.diff(result.diff)
            else:
                output = _agent_result_preview(result.output) if tid == "agent" else result.output
                resolved_mode, resolved_max = display_policy.resolve_display_mode(tid, output, result_ok=ok)
                if host._ui.via_events():
                    await host._ui.events.emit(ToolResultAppended(
                        tool_call_id=tool_event_id,
                        text=output,
                        display_mode=resolved_mode,
                        summary_max_lines=resolved_max,
                    ))
                elif tool_node:
                    display_output = output
                    if resolved_mode == ToolDisplayMode.SUMMARY:
                        lines = display_output.splitlines()
                        if len(lines) > resolved_max:
                            display_output = "\n".join(lines[:resolved_max]) + f"\n… +{len(lines) - resolved_max} more lines"
                    host._ui.dock.append_tool_result(
                        display_output,
                        parent=tool_node,
                        tool_call_id=tool_event_id,
                    )
                else:
                    host._ui.ui.tool_result(output)

            # maybe_persist before sanitize (persist needs raw content)
            llm_content = maybe_persist_tool_result(
                result.output, tool_event_id, tid,
                session_id=host._session.id if host._session else "default",
            )
            llm_content = sanitize_tool_message_content(llm_content, workspace=ctx.workspace)
            message = ToolMessage(
                content=llm_content,
                tool_call_id=cid,
            )
            return _ExecutedTool(message=message, result=result, tool_call=tc, todo_state=todo_state)

        async def execute_approved(approved: list[dict], *, serial: bool = False) -> list[_ExecutedTool]:
            if not approved:
                return []
            runnable, blocked = _split_runtime_guard_blocked_calls(approved, guard_state)
            unique_calls, duplicate_sources = _dedupe_repeated_read_calls(runnable)
            if serial:
                executed = []
                for tc in unique_calls:
                    executed.append(await execute_one(tc))
                restored = _restore_deduped_read_results(runnable, executed, duplicate_sources)
                return _restore_runtime_guard_blocked_results(approved, restored, blocked)

            agent_limit = _parallel_subagent_limit(host.config)
            agent_semaphore = asyncio.Semaphore(agent_limit)
            parallel_agent_count = sum(1 for tc in unique_calls if tc.get("name") == "agent")
            aggregate_status_id = ""
            show_parallel_status = agent_limit > 1 and parallel_agent_count > 1

            async def execute_one_limited(tc):
                if tc.get("name") == "agent":
                    async with agent_semaphore:
                        return await execute_one(tc)
                return await execute_one(tc)

            if show_parallel_status and host._ui.via_events():
                aggregate_status_id = f"parallel-subagents:{id(last)}:{id(unique_calls)}"
                await host._ui.events.emit(StatusUpdated(
                    status_id=aggregate_status_id,
                    label=f"Running {parallel_agent_count} child agents",
                    stage="working",
                ))

            executed = []
            try:
                executed = await asyncio.gather(*[execute_one_limited(tc) for tc in unique_calls])
            finally:
                if aggregate_status_id:
                    await host._ui.events.emit(StatusFinished(
                        status_id=aggregate_status_id,
                        label=f"Finished {parallel_agent_count} child agents",
                    ))
            restored = _restore_deduped_read_results(runnable, executed, duplicate_sources)
            return _restore_runtime_guard_blocked_results(approved, restored, blocked)

        executed: list[_ExecutedTool] = []
        denied: list[tuple[dict, str]] = []
        blocked_msgs: list[ToolMessage] = []
        pending = list(tool_calls)
        cycle_previous_todo_state = runtime_task_state.todo_state
        cycle_workflow_changed = False

        while pending:
            prefix, barrier, suffix = _split_at_first_barrier(pending)

            if prefix:
                approved, segment_denied = await _authorize_tool_calls(
                    host._authorize_tool_calls,
                    prefix,
                    runtime_persona=runtime_persona,
                    plan_mode=plan_mode,
                    session_id=session_id,
                    interaction_mode=interaction_mode,
                    workflow_runs=runtime_workflow_runs,
                )
                denied.extend(segment_denied)
                segment_executed = await execute_approved(approved)
                executed.extend(segment_executed)
                segment_update = _state_update_from_executed_tools(
                    segment_executed,
                    current_workflow_runs=runtime_workflow_runs,
                    current_workflow_route=runtime_task_state.workflow_route,
                    turn_count=turn_count,
                )
                cycle_workflow_changed = cycle_workflow_changed or "workflow_runs" in segment_update
                apply_state_update(segment_update)
                pending = ([barrier] if barrier is not None else []) + suffix
                continue

            if barrier is None:
                break

            approved, segment_denied = await _authorize_tool_calls(
                host._authorize_tool_calls,
                [barrier],
                runtime_persona=runtime_persona,
                plan_mode=plan_mode,
                session_id=session_id,
                interaction_mode=interaction_mode,
                workflow_runs=runtime_workflow_runs,
            )
            if segment_denied:
                denied.extend(segment_denied)
                blocked_msgs.extend(_blocked_after_barrier_messages(suffix, workspace, "denied"))
                break

            segment_executed = await execute_approved(approved, serial=True)
            executed.extend(segment_executed)
            segment_update = _state_update_from_executed_tools(
                segment_executed,
                current_workflow_runs=runtime_workflow_runs,
                current_workflow_route=runtime_task_state.workflow_route,
                turn_count=turn_count,
            )
            cycle_workflow_changed = cycle_workflow_changed or "workflow_runs" in segment_update
            apply_state_update(segment_update)
            if not segment_executed or not result_ok(segment_executed[-1].result):
                blocked_msgs.extend(_blocked_after_barrier_messages(suffix, workspace, "failed"))
                break

            pending = suffix

        # Clear on-failure tracking for this batch (full logic in Phase 2)
        host._needs_failure_check.clear()

        no_progress_decision = await _record_runtime_guard_outcomes(
            host,
            guard_state,
            executed,
            previous_todo_state=cycle_previous_todo_state,
            next_todo_state=runtime_task_state.todo_state,
            workflow_changed=cycle_workflow_changed,
            result_ok=result_ok,
        )

        if host._ui.via_events():
            await host._ui.events.drain()

        # Denied tools get error messages
        denied_msgs = [
            ToolMessage(
                content=sanitize_tool_message_content(reason, workspace=ctx.workspace),
                tool_call_id=tc.get("id", ""),
            )
            for tc, reason in denied
        ]
        original_order = {
            tc.get("id", ""): index
            for index, tc in enumerate(tool_calls)
            if tc.get("id", "")
        }
        tool_messages = [item.message for item in executed if item.message is not None] + denied_msgs + blocked_msgs
        tool_messages.sort(key=lambda msg: original_order.get(msg.tool_call_id, len(original_order)))
        compacted_messages = await _inline_compaction_messages(host, state.get("messages", []), executed)
        if compacted_messages:
            tool_messages = compacted_messages + tool_messages
        if (
            not denied_msgs
            and not blocked_msgs
            and _terminal_workflow_completed(
                executed,
                workflow_runs=runtime_workflow_runs,
                result_ok=result_ok,
            )
        ):
            state_update["should_continue"] = False
        if no_progress_decision.action == "terminate":
            tool_messages.append(AIMessage(content=no_progress_decision.message))
            state_update["should_continue"] = False
        return {
            "messages": tool_messages,
            **state_update,
        }

    @staticmethod
    def tool_result_ok(result) -> bool:
        metadata = getattr(result, "metadata", {}) or {}
        if metadata.get("error") or metadata.get("blocked") or metadata.get("timeout"):
            return False
        if "exit_code" in metadata:
            try:
                return int(metadata.get("exit_code") or 0) == 0
            except (TypeError, ValueError):
                return False
        return True


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
        message=ToolMessage(content=message, tool_call_id=tool_call.get("id", "")),
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
        ToolMessage(content=message, tool_call_id=call.get("id", ""))
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
    for item in executed:
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
        executed,
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
        status, wall_clock_decision = guard_state.wall_clock.record_check(
            label="voidx",
            latest_action=_latest_action_from_summary(summary),
        )
        await _emit_wall_clock_status(host, status)
        return wall_clock_decision
    return GuardDecision()


async def _emit_wall_clock_status(host, guidance: GuardGuidance | None) -> None:
    if guidance is None:
        return
    if host._ui.via_events():
        await host._ui.events.emit(StatusUpdated(
            status_id="runtime-guard:wall-clock",
            label=guidance.message,
            stage="working",
        ))
        return
    host._ui.ui.warn(guidance.message)


def _latest_action_from_summary(summary) -> str:
    if summary.only_tool:
        return summary.only_tool
    return ", ".join(summary.tool_names[:3])


def _submit_guard_guidance(host, guidance: GuardGuidance | None) -> None:
    if guidance is None:
        return
    submit = getattr(host, "submit_guidance", None)
    if callable(submit):
        submit(guidance.message)
        return
    pending = getattr(host, "_pending_guidance", None)
    if isinstance(pending, list):
        pending.append(guidance.message)


def _state_update_from_executed_tools(
    executed: list[_ExecutedTool],
    *,
    current_workflow_runs: object = (),
    current_workflow_route: object = None,
    turn_count: int = 0,
) -> dict:
    update: dict = {}
    merged_workflow_runs = _merge_workflow_runs_for_state(current_workflow_runs)
    workflow_runs_changed = False
    for item in executed:
        if item.tool_call.get("name") == "todo" and item.todo_state is not None:
            if item.todo_state.items:
                update["todo_state"] = item.todo_state.model_dump(mode="json")
            else:
                update["todo_state"] = None

        metadata = getattr(item.result, "metadata", {}) or {}
        raw = metadata.get("state_patch")
        if raw is None:
            continue
        patch = ToolStatePatch.model_validate(raw)
        data = patch.model_dump(mode="json")
        for field in patch.model_fields_set:
            if field == "intent":
                value = data.get(field)
                if value is not None:
                    update["task_intent"] = value.get("type") or "coding"
            elif field == "workflow_runs":
                route_limited = _explicit_advance_route_limited_runs(
                    item,
                    merged_workflow_runs,
                    current_workflow_route=current_workflow_route,
                    turn_count=turn_count,
                )
                if route_limited is not None:
                    merged_workflow_runs = route_limited
                    update["should_continue"] = False
                else:
                    merged_workflow_runs = _merge_workflow_runs_for_state(
                        merged_workflow_runs,
                        patch.workflow_runs,
                    )
                workflow_runs_changed = True
            elif field == "goal":
                update["current_goal"] = data.get(field)
            elif field == "persona":
                update["persona"] = data.get(field) or "coordinate"

    # Auto-advance: detect structured tool result signals and drive DAG
    # transitions without explicit advance_workflow.
    auto_events = _auto_advance_from_executed(executed, merged_workflow_runs)
    if auto_events:
        merged_workflow_runs, stop_after_auto = _advance_auto_events_for_route(
            merged_workflow_runs,
            auto_events,
            current_workflow_route=current_workflow_route,
            turn_count=turn_count,
        )
        workflow_runs_changed = True
        if stop_after_auto:
            update["should_continue"] = False

    if workflow_runs_changed:
        update["workflow_runs"] = merged_workflow_runs
    return update


async def _inline_compaction_messages(host, messages: list, executed: list[_ExecutedTool]) -> list:
    summary = _inline_compaction_summary(executed)
    if not summary:
        return []

    async def use_submitted_summary(_head_messages, _previous_summary):
        return summary

    # The LLM has already produced the summary via compact_context, so this
    # path bypasses budget gating and only reuses the coordinator's split,
    # persistence, and live-message replacement logic.
    result = await host._compaction_component().compact_for_live_state(
        list(messages),
        force=True,
        ask=False,
        include_summary_message=True,
        run_compaction_agent=use_submitted_summary,
        persist_compaction=host._persist_compaction,
    )
    if result is None:
        return []
    return [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *result.live_messages,
    ]


def _inline_compaction_summary(executed: list[_ExecutedTool]) -> str:
    for item in executed:
        metadata = getattr(item.result, "metadata", {}) or {}
        raw = metadata.get("inline_compaction")
        if not isinstance(raw, dict):
            continue
        summary = str(raw.get("summary") or "").strip()
        if summary:
            return summary
    return ""


def _auto_advance_from_executed(
    executed: list[_ExecutedTool],
    workflow_runs: list[WorkflowRunState],
) -> list:
    """Check executed tools for auto-advance signals and return events."""
    tool_items = []
    for item in executed:
        tool_items.append({
            "name": item.tool_call.get("name", ""),
            "result": item.result,
        })
    return auto_advance_events(tool_items, workflow_runs=workflow_runs)


def _explicit_advance_route_limited_runs(
    item: _ExecutedTool,
    workflow_runs: list[WorkflowRunState],
    *,
    current_workflow_route: object = None,
    turn_count: int = 0,
) -> list[WorkflowRunState] | None:
    if item.tool_call.get("name") != "advance_workflow":
        return None
    metadata = getattr(item.result, "metadata", {}) or {}
    transition = metadata.get("workflow_transition") or {}
    if not isinstance(transition, dict):
        return None
    workflow = str(transition.get("from") or "").strip().lower()
    condition = str(transition.get("condition") or "").strip().lower()
    target = workflow_transition_target(workflow, condition)
    if not _auto_event_satisfies_route_terminal(
        workflow,
        target,
        route_start=workflow_route_start(current_workflow_route),
        route_end=workflow_route_end(current_workflow_route),
    ):
        return None
    event = WorkflowStateEvent(
        workflow=workflow,
        kind=WorkflowStateEventKind.SATISFIED,
        ref="tool:advance_workflow",
        ok=True,
        summary=str(transition.get("summary") or ""),
        reason=str(transition.get("evidence") or ""),
        condition=condition,
    )
    return _satisfy_workflow_without_transition(workflow_runs, event, turn_count=turn_count)


def _advance_auto_events_for_route(
    workflow_runs: list[WorkflowRunState],
    auto_events: list,
    *,
    current_workflow_route: object = None,
    turn_count: int = 0,
) -> tuple[list[WorkflowRunState], bool]:
    route_start = workflow_route_start(current_workflow_route)
    route_end = workflow_route_end(current_workflow_route)
    runs = list(workflow_runs)
    should_stop = False
    for event in auto_events:
        target = workflow_transition_target(event.workflow, event.condition)
        if _auto_event_satisfies_route_terminal(
            event.workflow,
            target,
            route_start=route_start,
            route_end=route_end,
        ):
            runs = _satisfy_workflow_without_transition(runs, event, turn_count=turn_count)
            should_stop = True
            continue
        runs = advance_workflow_states(runs, [event])
        if _auto_event_should_stop_after_transition(
            event.ok,
            target,
            route_end=route_end,
        ):
            should_stop = True
    return runs, should_stop


def _auto_event_satisfies_route_terminal(
    workflow: str,
    target: str,
    *,
    route_start: str,
    route_end: str,
) -> bool:
    if not route_end or workflow != route_end:
        return False
    if not target:
        return True
    if route_start and route_start != route_end and workflow_path_reaches(target, route_end):
        return False
    return True


def _auto_event_should_stop_after_transition(
    ok: bool | None,
    target: str,
    *,
    route_end: str,
) -> bool:
    if route_end:
        return bool(target) and target != route_end and not workflow_path_reaches(target, route_end)
    return ok is False


def _satisfy_workflow_without_transition(
    workflow_runs: list[WorkflowRunState],
    event,
    *,
    turn_count: int = 0,
) -> list[WorkflowRunState]:
    target = str(getattr(event, "workflow", "") or "").strip().lower()
    updated: list[WorkflowRunState] = []
    for run in workflow_runs:
        copy = run.model_copy(deep=True)
        if copy.name == target and copy.status == WorkflowRunStatus.ACTIVE:
            copy.status = WorkflowRunStatus.SATISFIED
            copy.updated_turn = turn_count
            copy.blocked_reason = ""
            copy.evidence.append(
                WorkflowEvidence(
                    kind=event.kind.value,
                    ref=event.ref,
                    ok=event.ok,
                    summary=event.summary,
                    condition=event.condition,
                )
            )
        updated.append(copy)
    return updated


def _dedupe_repeated_read_calls(tool_calls: list[dict]) -> tuple[list[dict], dict[str, str]]:
    unique: list[dict] = []
    duplicate_sources: dict[str, str] = {}
    seen_reads: dict[str, str] = {}
    for call in tool_calls:
        if call.get("name") != "read":
            seen_reads.clear()
            unique.append(call)
            continue

        key = _read_call_key(call)
        call_id = str(call.get("id") or "")
        if key in seen_reads and call_id:
            duplicate_sources[call_id] = seen_reads[key]
            continue

        seen_reads[key] = call_id or "earlier read"
        unique.append(call)
    return unique, duplicate_sources


def _read_call_key(tool_call: dict) -> str:
    args = tool_call.get("args") or {}
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def _restore_deduped_read_results(
    original_calls: list[dict],
    unique_executed: list[_ExecutedTool],
    duplicate_sources: dict[str, str],
) -> list[_ExecutedTool]:
    if not duplicate_sources:
        return unique_executed

    restored: list[_ExecutedTool] = []
    unique_iter = iter(unique_executed)
    for call in original_calls:
        call_id = str(call.get("id") or "")
        source = duplicate_sources.get(call_id)
        if source is None:
            restored.append(next(unique_iter))
            continue
        output = f"Skipped duplicate read; same arguments already requested in tool call {source}."
        restored.append(_ExecutedTool(
            message=ToolMessage(content=output, tool_call_id=call_id),
            result=ToolResult(
                title="read: duplicate skipped",
                output=output,
                metadata={"deduplicated": True, "duplicate_of": source},
            ),
            tool_call=call,
        ))
    return restored


def _terminal_workflow_completed(
    executed: list[_ExecutedTool],
    *,
    workflow_runs: list[WorkflowRunState],
    result_ok: ToolResultOk,
) -> bool:
    if not executed:
        return False
    if any(not result_ok(item.result) for item in executed):
        return False

    saw_terminal_advance = False
    for item in executed:
        if item.tool_call.get("name") != "advance_workflow":
            continue
        metadata = getattr(item.result, "metadata", {}) or {}
        transition = metadata.get("workflow_transition") or {}
        if not isinstance(transition, dict):
            continue
        condition = str(transition.get("condition") or "")
        if is_workflow_terminal_condition(condition):
            saw_terminal_advance = True
            break
    if not saw_terminal_advance:
        return False
    return not any(run.status == WorkflowRunStatus.ACTIVE for run in workflow_runs)


def _merge_workflow_runs_for_state(*groups: object) -> list[WorkflowRunState]:
    merged: dict[str, WorkflowRunState] = {}
    for group in groups:
        items = group.values() if isinstance(group, dict) else group or []
        for item in items:
            try:
                run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
            except (TypeError, ValueError):
                continue
            merged[run.name] = run
    return list(merged.values())


def _parallel_subagent_limit(config) -> int:
    parallel = getattr(config, "parallel_subagents", None)
    if not bool(getattr(parallel, "enabled", False)):
        return 1
    raw = getattr(parallel, "max_concurrent", 4)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 4


def _agent_result_preview(text: object) -> str:
    raw = str(text)
    stripped = raw.strip()
    if not stripped:
        return raw

    lines = stripped.splitlines()
    visible = lines[:AGENT_RESULT_PREVIEW_LINES]
    omitted_lines = max(0, len(lines) - len(visible))

    preview = "\n".join(visible)
    omitted_chars = max(0, len(preview) - AGENT_RESULT_PREVIEW_CHARS)
    if omitted_chars:
        preview = preview[:AGENT_RESULT_PREVIEW_CHARS].rstrip()

    suffixes = []
    if omitted_lines:
        suffixes.append(f"{omitted_lines} more lines")
    if omitted_chars:
        suffixes.append(f"{omitted_chars} more chars")
    if suffixes:
        preview = f"{preview}\n... ({'; '.join(suffixes)} omitted; full result passed to voidx)"
    return preview


def _is_barrier_tool(tool_call: dict) -> bool:
    return tool_call.get("name") in {"clarify", "plan_checkpoint", "advance_workflow", "compact_context"}


def _split_at_first_barrier(tool_calls: list[dict]) -> tuple[list[dict], dict | None, list[dict]]:
    for index, tool_call in enumerate(tool_calls):
        if _is_barrier_tool(tool_call):
            return tool_calls[:index], tool_call, tool_calls[index + 1:]
    return tool_calls, None, []


def _blocked_after_barrier_messages(
    tool_calls: list[dict],
    workspace: str,
    outcome: str,
) -> list[ToolMessage]:
    return [
        ToolMessage(
            content=sanitize_tool_message_content(
                f"Blocked because a prior runtime barrier was {outcome}.",
                workspace=workspace,
            ),
            tool_call_id=tc.get("id", ""),
        )
        for tc in tool_calls
    ]


async def _authorize_tool_calls(
    authorize,
    tool_calls: list[dict],
    *,
    runtime_persona: str = "coordinate",
    plan_mode: bool,
    session_id: str,
    interaction_mode: str | None,
    workflow_runs: object,
):
    kwargs = {
        "runtime_persona": runtime_persona,
        "plan_mode": plan_mode,
        "session_id": session_id,
        "interaction_mode": interaction_mode,
    }
    kwargs["workflow_runs"] = workflow_runs
    try:
        signature = inspect.signature(authorize)
    except (TypeError, ValueError):
        return await authorize(tool_calls, **kwargs)
    params = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return await authorize(tool_calls, **kwargs)
    filtered = {key: value for key, value in kwargs.items() if key in params}
    return await authorize(tool_calls, **filtered)


def _make_interact_callback(app):
    if app is None:
        return None

    async def interact(request: UserInteraction) -> UserResponse:
        timeout = request.timeout
        if request.options:
            other_value = _other_choice_value(request.options)
            choices = [
                *request.options,
                ("Other (type your answer)", other_value, ""),
            ]
            result = await app.ask_choice(request.prompt, choices, timeout=timeout)
            if result == other_value:
                result = await app.ask_text(request.prompt, timeout=timeout)
                if result is None:
                    return UserResponse(value="", cancelled=True)
                return UserResponse(value=result, free_text=True)
        else:
            result = await app.ask_text(request.prompt, timeout=timeout)
            if result is None:
                return UserResponse(value="", cancelled=True)
            return UserResponse(value=result, free_text=True)
        if result is None:
            return UserResponse(value="", cancelled=True)
        return UserResponse(value=result)

    return interact


def _other_choice_value(options: list[tuple[str, str, str]]) -> str:
    used = {value for _, value, _ in options}
    value = _OTHER_VALUE_PREFIX
    index = 1
    while value in used:
        value = f"{_OTHER_VALUE_PREFIX}_{index}"
        index += 1
    return value


def _task_state_for_state(value: object, fallback: TaskState | None = None) -> TaskState:
    if isinstance(value, TaskState):
        return value.model_copy(deep=True)
    if isinstance(value, dict):
        try:
            return TaskState.model_validate(value)
        except ValueError:
            pass
    if fallback is not None:
        return fallback.model_copy(deep=True)
    return TaskState()


def _goal_for_state(value: object | None) -> GoalSpec | None:
    if value is None:
        return None
    if isinstance(value, GoalSpec):
        return value
    if isinstance(value, dict):
        try:
            return GoalSpec.model_validate(value)
        except ValueError:
            return None
    return None


def _todo_state_for_state(value: object | None) -> TodoRunState | None:
    if value is None:
        return None
    if isinstance(value, TodoRunState):
        return value
    if isinstance(value, dict):
        try:
            return TodoRunState.model_validate(value)
        except ValueError:
            return None
    return None


def _workflow_runs_for_state(value: object) -> list[WorkflowRunState]:
    runs: list[WorkflowRunState] = []
    items = value.values() if isinstance(value, dict) else value or []
    for item in items:
        try:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        except (TypeError, ValueError):
            continue
        runs.append(run)
    return runs


def _active_workflow_names(value: object) -> list[str]:
    names: list[str] = []
    for run in _workflow_runs_for_state(value):
        if run.status == WorkflowRunStatus.ACTIVE and run.name.strip():
            names.append(run.name.strip())
    return names
