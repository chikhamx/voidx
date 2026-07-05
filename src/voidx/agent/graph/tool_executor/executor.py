from __future__ import annotations

import time
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, ToolMessage

from voidx.agent.graph.runtime import current_parent_tool_call_id
from voidx.agent.graph.workflow_utils import active_workflow_names
from voidx.agent.todo_state import todo_run_state_from_result
from voidx.agent.task_state import goal_label, goal_type_from_join
from voidx.agent.tool_messages import sanitize_tool_message_content
from voidx.agent.tool_result_storage import maybe_persist_tool_result
from voidx.agent.graph.todo_events import todo_updated_event
from voidx.runtime.ui import (
    DEFAULT_DISPLAY_RULES,
    ToolDisplayPolicy,
    WarningAppended,
)
from voidx.tools.service import ToolContext, ToolResult

from .types import ToolResultOk, _ExecutedTool, _task_state_for_state, _tool_result_ok
from .guards import (
    _runtime_guard_state,
    _runtime_guard_tool_messages,
    _record_runtime_guard_outcomes,
)
from .workflow import (
    _state_update_from_executed_tools,
    _inline_compaction_messages,
    _terminal_workflow_completed,
)
from .helpers import (
    _apply_state_update,
    _execute_approved_batch,
    _authorize_tool_calls,
    _split_at_first_barrier,
    _blocked_after_barrier_messages,
    _agent_result_preview,
    _make_interact_callback,
    _requires_workspace_write_lock,
    _workspace_write_lock_manager,
)
from .ui import (
    notify_tool_started,
    notify_tool_result,
    notify_tool_diff,
    notify_tool_failure,
    notify_tool_text_output,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphToolExecutionHost


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

        runtime_persona_ref = [runtime_persona, runtime_task_intent]
        runtime_task_state_ref = [runtime_task_state, runtime_goal, runtime_workflow_runs]

        def make_context() -> ToolContext:
            def _add_extra_path(path: str) -> None:
                if path not in host._permission.sandbox_workspace_write:
                    host._permission.sandbox_workspace_write.append(path)

            return ToolContext(
                workspace=workspace,
                session_id=session_id,
                persona=runtime_persona_ref[0],
                interaction_mode=interaction_mode or ("plan" if plan_mode else "auto"),
                task_intent=str(runtime_persona_ref[1] or "coding"),
                goal_type=goal_type_from_join(
                    runtime_task_state_ref[0].workflow_route.join
                    if runtime_task_state_ref[0].workflow_route is not None
                    else None
                ),
                goal_target=goal_label(runtime_task_state_ref[1]),
                turn_count=turn_count,
                active_workflow_names=active_workflow_names(runtime_task_state_ref[2]),
                workflow_runs=runtime_task_state_ref[2],
                workflow_route=runtime_task_state_ref[0].workflow_route.model_dump(mode="json")
                if runtime_task_state_ref[0].workflow_route is not None
                else None,
                file_mtimes=host._file_mtimes,
                file_read_coverage=host._file_read_coverage,
                workflow_repeat_tracker=host._workflow_repeat_tracker,
                mcp_manager=getattr(host, "_mcp_manager", None),
                lsp_manager=getattr(host, "_lsp_manager", None),
                sandbox_mode=host._permission.sandbox_mode,
                sandbox_extra_paths=host._permission.sandbox_workspace_write,
                interact=_make_interact_callback(getattr(host, "_app", None)),
                add_extra_path=_add_extra_path,
            )

        ctx = make_context()

        def apply_state_update(update: dict) -> None:
            nonlocal ctx
            _apply_state_update(
                update,
                host=host,
                state_update=state_update,
                runtime_task_state=runtime_task_state_ref,
                runtime_persona_ref=runtime_persona_ref,
            )
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

        async def execute_one(tc):
            tid = tc["name"]
            targs = tc.get("args", {})
            cid = tc.get("id", "")
            tool_event_id = cid or f"{tid}:{id(tc)}"

            tool_node = await notify_tool_started(host, tc, display_policy)

            t0 = time.monotonic()
            ok = True
            try:
                host._ui.session_tracker.capture_tool_call(tid, targs, ctx.workspace, ctx.sandbox_extra_paths)
                parent_tool_token = current_parent_tool_call_id.set(tool_event_id)
                lock_manager = _workspace_write_lock_manager(host) if _requires_workspace_write_lock(tc) else None
                lock_acquired = False
                try:
                    if lock_manager is not None:
                        lock_acquired = await lock_manager.acquire_workspace_write_lock(session_id)
                        if not lock_acquired:
                            result = ToolResult(
                                output="Workspace write lock acquisition cancelled before tool start.",
                                metadata={"blocked": True, "error": True},
                            )
                        else:
                            result = await host.tools.execute_tool(tid, targs, ctx)
                    else:
                        result = await host.tools.execute_tool(tid, targs, ctx)
                finally:
                    if lock_acquired and lock_manager is not None:
                        lock_manager.release_workspace_write_lock(session_id)
                    current_parent_tool_call_id.reset(parent_tool_token)
                ok = result_ok(result)
            except Exception as e:
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

            if not ok:
                failure_tc = host._needs_failure_check.get(cid, None)
                if failure_tc and host._permission.approval_policy == "on-failure":
                    host._notify_tool_failure(failure_tc, result)
            elif ok:
                host._clear_failure_check(cid)

            todo_state = todo_run_state_from_result(result) if tid == "todo" and ok else None
            todo_meta = getattr(result, "metadata", {}) or {} if tid == "todo" and ok else {}
            is_todo_read = todo_meta.get("todo_op") == "read"
            if host._ui.via_events() and tid == "todo" and not is_todo_read:
                todo_event = todo_updated_event(result)
                if todo_event is not None:
                    await host._ui.events.emit(todo_event)
                elif ok:
                    await host._ui.events.emit(WarningAppended(
                        message="Todo update ignored: tool returned malformed metadata.",
                    ))
            elif tid == "todo" and ok and todo_state is None and not is_todo_read:
                host._ui.ui.warn("Todo update ignored: tool returned malformed metadata.")

            notify_tool_failure(host, tc, result, display_policy.rule_for(tid).mode, tool_event_id, ok)

            await notify_tool_result(host, tc, result, ok, elapsed, display_policy, tool_node)

            if getattr(result, "diff", None) and ok:
                host._ui.session_tracker.record_diff(result.diff)
                await notify_tool_diff(host, result, tool_event_id, tool_node)
            else:
                ui_output = _agent_result_preview(result.output) if tid == "agent" else (result.display or result.output)
                await notify_tool_text_output(host, ui_output, tid, tool_event_id, tool_node, display_policy, ok)

            llm_content = maybe_persist_tool_result(
                result.output, tool_event_id, tid,
                session_id=host._session.id if host._session else "default",
            )
            next_step_hint = getattr(result, "next_step_hint", "").strip()
            if next_step_hint:
                llm_content = f"{llm_content}\n\nNext step hint: {next_step_hint}"
            llm_content = sanitize_tool_message_content(llm_content, workspace=ctx.workspace)
            message = ToolMessage(
                content=llm_content,
                tool_call_id=cid,
                status="success" if ok else "error",
            )
            return _ExecutedTool(message=message, result=result, tool_call=tc, todo_state=todo_state)

        async def execute_approved(approved: list[dict], *, serial: bool = False) -> list[_ExecutedTool]:
            return await _execute_approved_batch(
                approved,
                host=host,
                guard_state=guard_state,
                execute_one_fn=execute_one,
                serial=serial,
            )

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
                    runtime_persona=runtime_persona_ref[0],
                    plan_mode=plan_mode,
                    session_id=session_id,
                    interaction_mode=interaction_mode,
                    workflow_runs=runtime_task_state_ref[2],
                )
                denied.extend(segment_denied)
                segment_executed = await execute_approved(approved)
                executed.extend(segment_executed)
                segment_update = _state_update_from_executed_tools(
                    segment_executed,
                    current_workflow_runs=runtime_task_state_ref[2],
                    current_workflow_route=runtime_task_state_ref[0].workflow_route,
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
                runtime_persona=runtime_persona_ref[0],
                plan_mode=plan_mode,
                session_id=session_id,
                interaction_mode=interaction_mode,
                workflow_runs=runtime_task_state_ref[2],
            )
            if segment_denied:
                denied.extend(segment_denied)
                blocked_msgs.extend(_blocked_after_barrier_messages(suffix, workspace, "denied"))
                break

            segment_executed = await execute_approved(approved, serial=True)
            executed.extend(segment_executed)
            segment_update = _state_update_from_executed_tools(
                segment_executed,
                current_workflow_runs=runtime_task_state_ref[2],
                current_workflow_route=runtime_task_state_ref[0].workflow_route,
                turn_count=turn_count,
            )
            cycle_workflow_changed = cycle_workflow_changed or "workflow_runs" in segment_update
            apply_state_update(segment_update)
            if not segment_executed or not result_ok(segment_executed[-1].result):
                blocked_msgs.extend(_blocked_after_barrier_messages(suffix, workspace, "failed"))
                break
            pending = suffix

        host._needs_failure_check.clear()

        no_progress_decision = await _record_runtime_guard_outcomes(
            host,
            guard_state,
            executed,
            previous_todo_state=cycle_previous_todo_state,
            next_todo_state=runtime_task_state_ref[0].todo_state,
            workflow_changed=cycle_workflow_changed,
            result_ok=result_ok,
        )

        if host._ui.via_events():
            await host._ui.events.drain()

        denied_msgs = [
            ToolMessage(
                content=sanitize_tool_message_content(reason, workspace=ctx.workspace),
                tool_call_id=tc.get("id", ""),
                status="error",
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
                workflow_runs=runtime_task_state_ref[2],
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

    tool_result_ok = staticmethod(_tool_result_ok)
