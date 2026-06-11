"""Composition component for the agent graph tool execution node."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, ToolMessage

from voidx.diffing import diff_stat
from voidx.agent.graph.runtime import current_parent_tool_call_id
from voidx.agent.graph.todo_events import todo_updated_event
from voidx.agent.todo_state import apply_todo_state_to_host, todo_run_state_from_result
from voidx.agent.task_state import TodoRunState, ToolStatePatch
from voidx.agent.tool_messages import sanitize_tool_message_content
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus, advance_workflow_states
from voidx.tools.base import ToolContext, UserInteraction, UserResponse
from voidx.ui.output.console import _fmt_args, _title
from voidx.ui.output.events.schema import (
    FileChangeAppended,
    StatusFinished,
    StatusUpdated,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    WarningAppended,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphToolExecutionHost


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
        agent_name = state.get("agent", "orchestrator")
        session_id = host._session.id if host._session else "default"
        plan_mode = state.get("plan_mode", False)
        interaction_mode = state.get("interaction_mode")
        workspace = state.get("workspace", host._workspace)
        runtime_task_intent = state.get("task_intent", "chat")
        runtime_pending_approval = _dump_pending_approval(state.get("pending_approval"))
        runtime_goal = state.get("goal", "")
        runtime_workflow_runs = _workflow_runs_for_state(state.get("workflow_runs", []) or [])
        state_update: dict = {}

        def make_context() -> ToolContext:
            return ToolContext(
                workspace=workspace,
                session_id=session_id,
                agent=agent_name,
                interaction_mode=interaction_mode or ("plan" if plan_mode else "auto"),
                task_intent=str(runtime_task_intent or "chat"),
                pending_approval=runtime_pending_approval,
                goal=str(runtime_goal or ""),
                goal_turn_count=state.get("goal_turn_count", 0),
                active_workflow_names=_active_workflow_names(runtime_workflow_runs),
                workflow_runs=runtime_workflow_runs,
                file_mtimes=host._file_mtimes,
                mcp_manager=getattr(host, "_mcp_manager", None),
                lsp_manager=getattr(host, "_lsp_manager", None),
                sandbox_mode=host._permission.sandbox_mode,
                sandbox_extra_paths=host._permission.sandbox_workspace_write,
                interact=_make_interact_callback(getattr(host, "_app", None)),
            )

        ctx = make_context()

        def apply_state_update(update: dict) -> None:
            nonlocal ctx, runtime_goal, runtime_pending_approval, runtime_workflow_runs, runtime_task_intent
            if not update:
                return
            state_update.update(update)
            if "todo_state" in update:
                apply_todo_state_to_host(host, update.get("todo_state"))
            if "task_intent" in update:
                runtime_task_intent = update.get("task_intent") or "chat"
            if "pending_approval" in update:
                runtime_pending_approval = _dump_pending_approval(update.get("pending_approval"))
            if "goal" in update:
                runtime_goal = update.get("goal") or ""
            if "workflow_runs" in update:
                runtime_workflow_runs = _workflow_runs_for_state(update.get("workflow_runs") or [])
            ctx = make_context()

        tool_calls = last.tool_calls

        # ── Phase 2: parallel execution of all approved tools ────────

        async def execute_one(tc):
            tid = tc["name"]
            targs = tc.get("args", {})
            cid = tc.get("id", "")

            tool_event_id = cid or f"{tid}:{id(tc)}"
            tool_node = None
            if host._ui.via_events():
                gerund = _title(host._ui.ui._TOOL_GERUND.get(tid, tid + "ing"))
                tool_node = await host._ui.events.request(ToolStarted(
                    tool_call_id=tool_event_id,
                    tool_name=tid,
                    label=gerund,
                    args=_fmt_args(targs),
                    raw_args=targs,
                ))
                if host._ui.dock.active and host._ui.dock.current_agent is not None:
                    host._turn_node = host._ui.dock.current_agent
            elif host._ui.dock.active:
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
                from voidx.tools.base import ToolResult
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
                await host._ui.events.emit(ToolFinished(
                    tool_call_id=tool_event_id,
                    label=_title(tid),
                    elapsed=elapsed,
                    ok=ok,
                ))
            elif tool_node:
                host._ui.dock.finish_tool_node(tool_node, _title(tid), elapsed, ok)
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
            elif host._debug or tid == "agent":
                output = _agent_result_preview(result.output) if tid == "agent" else result.output
                if host._ui.via_events():
                    await host._ui.events.emit(ToolResultAppended(
                        tool_call_id=tool_event_id,
                        text=output,
                    ))
                elif tool_node:
                    host._ui.dock.append_tool_result(
                        output,
                        parent=tool_node,
                        tool_call_id=tool_event_id,
                    )
                else:
                    host._ui.ui.tool_result(output)

            message = None if tid == "todo" else ToolMessage(
                content=sanitize_tool_message_content(result.output, workspace=ctx.workspace),
                tool_call_id=cid,
            )
            return _ExecutedTool(message=message, result=result, tool_call=tc, todo_state=todo_state)

        async def execute_approved(approved: list[dict], *, serial: bool = False) -> list[_ExecutedTool]:
            if not approved:
                return []
            if serial:
                executed = []
                for tc in approved:
                    executed.append(await execute_one(tc))
                return executed

            agent_limit = _parallel_subagent_limit(host.config)
            agent_semaphore = asyncio.Semaphore(agent_limit)
            parallel_agent_count = sum(1 for tc in approved if tc.get("name") == "agent")
            aggregate_status_id = ""
            show_parallel_status = agent_limit > 1 and parallel_agent_count > 1

            async def execute_one_limited(tc):
                if tc.get("name") == "agent":
                    async with agent_semaphore:
                        return await execute_one(tc)
                return await execute_one(tc)

            if show_parallel_status and host._ui.via_events():
                aggregate_status_id = f"parallel-subagents:{id(last)}:{id(approved)}"
                await host._ui.events.emit(StatusUpdated(
                    status_id=aggregate_status_id,
                    label=f"Running {parallel_agent_count} child agents",
                    stage="working",
                ))

            executed = []
            try:
                executed = await asyncio.gather(*[execute_one_limited(tc) for tc in approved])
            finally:
                if aggregate_status_id:
                    await host._ui.events.emit(StatusFinished(
                        status_id=aggregate_status_id,
                        label=f"Finished {parallel_agent_count} child agents",
                    ))
            return executed

        executed: list[_ExecutedTool] = []
        denied: list[tuple[dict, str]] = []
        blocked_msgs: list[ToolMessage] = []
        pending = list(tool_calls)

        while pending:
            prefix, barrier, suffix = _split_at_first_barrier(pending)

            if prefix:
                approved, segment_denied = await _authorize_tool_calls(
                    host._authorize_tool_calls,
                    prefix,
                    agent_name=agent_name,
                    plan_mode=plan_mode,
                    session_id=session_id,
                    interaction_mode=interaction_mode,
                    workflow_runs=runtime_workflow_runs,
                )
                denied.extend(segment_denied)
                segment_executed = await execute_approved(approved)
                executed.extend(segment_executed)
                apply_state_update(
                    _state_update_from_executed_tools(
                        segment_executed,
                        current_workflow_runs=runtime_workflow_runs,
                    )
                )
                pending = ([barrier] if barrier is not None else []) + suffix
                continue

            if barrier is None:
                break

            approved, segment_denied = await _authorize_tool_calls(
                host._authorize_tool_calls,
                [barrier],
                agent_name=agent_name,
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
            apply_state_update(
                _state_update_from_executed_tools(
                    segment_executed,
                    current_workflow_runs=runtime_workflow_runs,
                )
            )
            if not segment_executed or not result_ok(segment_executed[-1].result):
                blocked_msgs.extend(_blocked_after_barrier_messages(suffix, workspace, "failed"))
                break

            pending = suffix

        # Clear on-failure tracking for this batch (full logic in Phase 2)
        host._needs_failure_check.clear()

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


def _state_update_from_executed_tools(
    executed: list[_ExecutedTool],
    *,
    current_workflow_runs: object = (),
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
        if raw is None and isinstance(metadata.get("on_intent"), dict):
            raw = metadata["on_intent"].get("state_patch")
        if raw is None:
            continue
        patch = ToolStatePatch.model_validate(raw)
        data = patch.model_dump(mode="json")
        for field in patch.model_fields_set:
            if field == "task_intent":
                value = data.get(field)
                if value is not None:
                    update["task_intent"] = value
            elif field == "pending_approval":
                update["pending_approval"] = data.get(field)
            elif field == "workflow_runs":
                merged_workflow_runs = _merge_workflow_runs_for_state(
                    merged_workflow_runs,
                    patch.workflow_runs,
                )
                workflow_runs_changed = True
            else:
                update[field] = data.get(field)

    # Auto-advance: detect review_has_issues / failed_implementation from
    # tool results and drive DAG transitions without explicit advance_workflow.
    auto_events = _auto_advance_from_executed(executed, merged_workflow_runs)
    if auto_events:
        merged_workflow_runs = advance_workflow_states(
            merged_workflow_runs, auto_events,
        )
        workflow_runs_changed = True

    if workflow_runs_changed:
        update["workflow_runs"] = merged_workflow_runs
    return update


def _auto_advance_from_executed(
    executed: list[_ExecutedTool],
    workflow_runs: list[WorkflowRunState],
) -> list:
    """Check executed tools for auto-advance signals and return events."""
    from voidx.workflow.auto_advance import auto_advance_events

    tool_items = []
    for item in executed:
        tool_items.append({
            "name": item.tool_call.get("name", ""),
            "result": item.result,
        })
    return auto_advance_events(tool_items, workflow_runs=workflow_runs)


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
        preview = f"{preview}\n... ({'; '.join(suffixes)} omitted; full result passed to orchestrator)"
    return preview


def _is_barrier_tool(tool_call: dict) -> bool:
    return tool_call.get("name") in {"on_intent", "clarify", "plan_checkpoint", "advance_workflow"}


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
    agent_name: str,
    plan_mode: bool,
    session_id: str,
    interaction_mode: str | None,
    workflow_runs: object,
):
    kwargs = {
        "agent_name": agent_name,
        "plan_mode": plan_mode,
        "session_id": session_id,
        "interaction_mode": interaction_mode,
    }
    try:
        signature = inspect.signature(authorize)
    except (TypeError, ValueError):
        kwargs["workflow_runs"] = workflow_runs
    else:
        params = signature.parameters
        if "workflow_runs" in params or any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in params.values()
        ):
            kwargs["workflow_runs"] = workflow_runs
    return await authorize(tool_calls, **kwargs)


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


def _dump_pending_approval(value: object | None) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
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
