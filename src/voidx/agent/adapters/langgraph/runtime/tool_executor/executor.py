from __future__ import annotations

from voidx.agent.adapters.langgraph.ui_events import AssistantStreamCommitted, AssistantStreamUpdated, StatusFinished, StatusUpdated, WarningAppended
from voidx.agent.adapters.langgraph.display_policy import DEFAULT_DISPLAY_RULES, ToolDisplayPolicy

import asyncio
import time
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, ToolMessage

from voidx.observability.tool_log import log_tool_event
from voidx.agent.domain.task.intent import InteractionMode
from voidx.agent.adapters.langgraph.runtime.runtime import current_parent_tool_call_id
from voidx.agent.application.workflow_utils import active_workflow_names
from voidx.agent.application.todo_state import todo_run_state_from_result
from voidx.agent.domain.task.state import goal_label, goal_type_from_join
from voidx.agent.application.tool_messages import sanitize_tool_message_content
from voidx.agent.adapters.tools.result_storage import maybe_persist_tool_result
from voidx.agent.adapters.langgraph.runtime.todo_events import todo_updated_event
from voidx.agent.ports.ui import UiEventTimeout
from voidx.agent.adapters.tools.automation.loop import LoopTool
from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime
from voidx.agent.adapters.tools.plugins import bind_agent_tool_runtime
from voidx.tooling.domain.risk import ApprovedToolRisk
from voidx.tooling.application.execution import (
    AuthorizationRuntime,
    CallbackInteractionPort,
)
from voidx.tooling.domain.file_tracking import FileStateStore
from voidx.tooling.domain.result import ToolResult

from .types import ToolResultOk, _ExecutedTool, _task_state_for_state, _tool_result_ok
from .guards import (
    _runtime_guard_state,
    _runtime_guard_tool_messages,
    _record_runtime_guard_outcomes,
)
from .workflow import (
    _state_update_from_executed_tools,
    _inline_compaction_messages,
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
    _infrastructure_skipped_tool,
)
from .ui import (
    notify_tool_started,
    notify_tool_result,
    notify_tool_diff,
    notify_tool_failure,
    notify_tool_text_output,
)

def _loop_iteration_committed(loop_controller) -> bool:
    return _loop_iteration_decision(loop_controller) is not None


def _loop_iteration_decision(loop_controller):
    if loop_controller is None:
        return None
    return loop_controller.final_decision()


def _goal_phase_committed(turn_context) -> str | None:
    """Skip reason once the goal phase's terminal submission landed, else None."""
    if turn_context is None:
        return None
    phase = getattr(turn_context, "goal_phase", "")
    if phase == "intake":
        controller = getattr(turn_context, "goal_intake_controller", None)
        if controller is None:
            return None
        if controller.final_spec() is not None or getattr(controller, "cancelled", False):
            return "was skipped after goal intake submission"
        return None
    if phase == "evaluator":
        controller = getattr(turn_context, "goal_controller", None)
        if controller is not None and controller.final_decision() is not None:
            return "was skipped after goal decision"
    return None


def _loop_commit_terminal_message(loop_controller, source_msg: AIMessage) -> AIMessage | None:
    if str(source_msg.content or "").strip() or not _has_loop_commit_call(source_msg):
        return None
    decision = _loop_iteration_decision(loop_controller)
    summary = str(getattr(decision, "summary", "") or "").strip()
    if not summary:
        return None
    return AIMessage(content=summary)


def _has_loop_commit_call(source_msg: AIMessage) -> bool:
    for call in getattr(source_msg, "tool_calls", None) or []:
        if not isinstance(call, dict) or call.get("name") != "loop":
            continue
        args = call.get("args")
        if isinstance(args, dict) and args.get("operation") == "commit":
            return True
    return False


async def _publish_loop_terminal_message(host, message: AIMessage) -> None:
    text = str(message.content or "").strip()
    if not text:
        return
    if host._ui.via_events():
        await host._ui.events.emit(AssistantStreamUpdated(text=text, phase="text"))
        await host._ui.events.emit(AssistantStreamCommitted())
    else:
        host._ui.ui.print(text)


async def _apply_pending_turn_stop_commit(host: Any, result: dict) -> dict:
    pending_stop = getattr(host, "_pending_turn_stop_commit", None)
    if pending_stop is None:
        return result

    host._pending_turn_stop_commit = None
    terminal_msg = pending_stop.get("terminal_msg")
    if not isinstance(terminal_msg, AIMessage):
        return result

    messages = list(result.get("messages") or [])
    if bool(pending_stop.get("terminal_msg_visible", True)):
        await _publish_loop_terminal_message(host, terminal_msg)
    messages.append(terminal_msg)
    return {
        **result,
        "messages": messages,
        "should_continue": False,
        "turn_state": "committed",
    }


UI_EVENT_BUS_TIMEOUT_KIND = "ui_event_bus_timeout"
TOOL_HEARTBEAT_INITIAL_SECONDS = 15.0
TOOL_HEARTBEAT_INTERVAL_SECONDS = 15.0


class ToolExecutorAdapter:
    """Executes approved tool calls for a graph host."""

    def __init__(self, host: Any) -> None:
        self.host = host

    async def execute_tools(
        self,
        state,
        *,
        tool_result_ok: ToolResultOk | None = None,
    ) -> dict:
        host = self.host
        try:
            return await self._execute_tools_body(state, tool_result_ok=tool_result_ok)
        except Exception:
            host._pending_turn_stop_commit = None
            raise

    async def _execute_tools_body(
        self,
        state,
        *,
        tool_result_ok: ToolResultOk | None = None,
    ) -> dict:
        host = self.host
        result_ok = tool_result_ok or self.tool_result_ok
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            host._pending_turn_stop_commit = None
            return {}

        if host._ui.dock.active and host._ui.dock.current_agent is not None:
            host._turn_node = host._ui.dock.current_agent

        host._current_messages = state["messages"]
        runtime_persona = state.get("persona", "coordinate")
        session_id = host._session.id if host._session else "default"
        plan_mode = state.get("plan_mode", False)
        interaction_mode = state.get("interaction_mode")
        from voidx.agent.adapters.langgraph.runtime.thread_context import current_thread_execution_state
        thread_state = current_thread_execution_state()
        if thread_state is None:
            raise RuntimeError("tool execution requires bound TurnExecutionContext")
        workspace = thread_state.workspace
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

        permission = host._permission
        agent_runtime = AgentToolRuntime(
            loop_control=getattr(thread_state.turn_context, "loop_controller", None),
            goal_control=getattr(thread_state.turn_context, "goal_controller", None),
            goal_intake=getattr(thread_state.turn_context, "goal_intake_controller", None),
            loop_intake=getattr(thread_state.turn_context, "loop_intake_controller", None),
            workflow_repeat_state=host._workflow_repeat_tracker,
            subagent_transport=getattr(host, "agent_gateway", None),
            run_id=(
                getattr(host, "agent_gateway", None).ensure_root(session_id)
                if getattr(host, "agent_gateway", None) is not None
                else ""
            ),
            interaction=_make_interact_callback(host._ui, host._ui),
            events=getattr(host, "tool_ui_events", None),
            access_grants=permission.get_access_grants,
            revocation_epoch=lambda: permission.revocation_epoch,
        )
        bind_agent_tool_runtime(host.tools, agent_runtime)

        authorization_runtime = AuthorizationRuntime(
            read_files=list(permission.sandbox_readable_files),
            read_dirs=list(permission.sandbox_readable_dirs),
            write_files=list(permission.sandbox_writable_files),
            write_dirs=list(permission.sandbox_writable_dirs),
            access_grants_reader=permission.get_access_grants,
            revocation_epoch_reader=lambda: permission.revocation_epoch,
            grant_writer=permission.add_grant,
            target_locker=permission.acquire_grant_targets,
            execution_lease_factory=permission.execution_lease_for_tool,
            interaction=CallbackInteractionPort(
                _make_interact_callback(host._ui, host._ui)
            ),
        )
        host._scoped_tools_binder(
            host.tools,
            authorization=authorization_runtime,
            files=FileStateStore(
                mtimes=host._file_mtimes,
                read_coverage=host._file_read_coverage,
            ),
            process_sandbox=getattr(permission, "process_sandbox", None),
            lsp_operations=host._lsp_operations,
            format_after_edit_enabled=host.config.lsp_format_after_edit,
        )

        def make_context() -> ToolContext:
            agent_runtime.task_intent = str(runtime_persona_ref[1] or "coding")
            agent_runtime.goal_type = goal_type_from_join(
                runtime_task_state_ref[0].workflow_route.join
                if runtime_task_state_ref[0].workflow_route is not None
                else None
            )
            agent_runtime.goal_target = goal_label(runtime_task_state_ref[1])
            agent_runtime.active_workflow_names = active_workflow_names(runtime_task_state_ref[2])
            agent_runtime.workflow_runs = runtime_task_state_ref[2]
            agent_runtime.workflow_route = (
                runtime_task_state_ref[0].workflow_route.model_dump(mode="json")
                if runtime_task_state_ref[0].workflow_route is not None
                else None
            )
            agent_runtime.goal_phase = getattr(thread_state.turn_context, "goal_phase", "work")
            return ToolContext(
                workspace=workspace,
                runtime=agent_runtime,
                session_id=session_id,
                persona=runtime_persona_ref[0],
                interaction_mode=interaction_mode or (InteractionMode.PLAN.value if plan_mode else InteractionMode.AUTO.value),
                turn_count=turn_count,
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
            return await _apply_pending_turn_stop_commit(host, result)

        async def execute_one(tc):
            tid = tc["name"]
            targs = tc.get("args", {})
            from voidx.agent.adapters.langgraph.runtime.thread_context import current_thread_execution_state
            state_context = current_thread_execution_state()
            tool_policy = getattr(state_context, "tool_policy", None) if state_context else None
            if tool_policy is not None:
                decision = tool_policy.check_tool_call(tid, targs)
                if not decision.allowed:
                    return _ExecutedTool(
                        message=ToolMessage(
                            content=f"Tool denied: {decision.reason}",
                            tool_call_id=tc.get("id", ""),
                            status="error",
                        ),
                        result=ToolResult(
                            output=f"Tool denied: {decision.reason}",
                            metadata={"error": True, "tool_denied": True, "reason": decision.reason},
                        ),
                        tool_call=tc,
                        todo_state=None,
                        runtime_guard_eligible=False,
                    )
            cid = tc.get("id", "")
            tool_event_id = cid or f"{tid}:{id(tc)}"


            try:
                tool_node = await notify_tool_started(host, tc, display_policy)
            except UiEventTimeout:
                return _ExecutedTool(
                    message=ToolMessage(
                        content=sanitize_tool_message_content(
                            f"Tool notification timed out: UI event bus stalled for {tid}. "
                            "Turn terminated to prevent hang.",
                            workspace=ctx.workspace,
                        ),
                        tool_call_id=cid,
                        status="error",
                    ),
                    result=ToolResult(
                        output="UI event bus timeout",
                        metadata={
                            "error": True,
                            "timeout": True,
                            "error_kind": UI_EVENT_BUS_TIMEOUT_KIND,
                            "timeout_source": "ui_event_bus",
                        },
                    ),
                    tool_call=tc,
                    todo_state=None,
                    terminal_reason=UI_EVENT_BUS_TIMEOUT_KIND,
                    runtime_guard_eligible=False,
                )

            t0 = time.monotonic()
            ok = True
            heartbeat_task: asyncio.Task[None] | None = None
            if host._ui.via_events():
                heartbeat_task = asyncio.create_task(
                    _emit_tool_heartbeat(
                        host,
                        tid=tid,
                        tool_event_id=tool_event_id,
                        started_at=t0,
                    ),
                    name=f"voidx-tool-heartbeat:{tid}",
                )
            try:
                host._ui.session_tracker.capture_tool_call(
                    tid,
                    targs,
                    ctx.workspace,
                    authorization_runtime.sandbox_paths(write=False),
                )
                parent_tool_token = current_parent_tool_call_id.set(tool_event_id)
                lock_manager = _workspace_write_lock_manager(host) if _requires_workspace_write_lock(tc) else None
                lock_acquired = False
                lease_factory = getattr(host._permission, "execution_lease_for_tool", None)

                async def run_authorized_tool() -> ToolResult:
                    nonlocal lock_acquired
                    if lock_manager is not None:
                        lock_acquired = await lock_manager.acquire_workspace_write_lock(session_id)
                        if not lock_acquired:
                            return ToolResult(
                                output="Workspace write lock acquisition cancelled before tool start.",
                                metadata={"blocked": True, "error": True},
                            )
                    tool_ctx = ctx.model_copy(
                        update={"approved_tool_risks": tuple(_approved_tool_risks_for_call(tc))}
                    )
                    if tid == "loop" and tool_ctx.runtime.loop_control is not None:
                        return await LoopTool().execute(targs, tool_ctx)
                    return await host.tools.execute_tool(tid, targs, tool_ctx)

                try:
                    if lease_factory is not None:
                        async with lease_factory(tid):
                            result = await run_authorized_tool()
                    else:
                        result = await run_authorized_tool()
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
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
                    await host._ui.events.emit(StatusFinished(
                        status_id=f"tool-heartbeat:{tool_event_id}",
                        remove=True,
                    ))
            elapsed = time.monotonic() - t0

            if not ok:
                failure_tc = host._needs_failure_check.get(cid, None)
                if failure_tc and host._permission.approval_policy == "on-failure":
                    host._notify_tool_failure(failure_tc, result)
            elif ok:
                host._clear_failure_check(cid)
                record_success = getattr(host, "_record_successful_tool_call", None)
                if record_success is not None:
                    record_success(tc)

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
                if tid == "agent":
                    # Prefer explicit UI display; successful spawn has none (subagent tree covers it).
                    ui_output = result.display or (
                        _agent_result_preview(result.output) if ok else result.output
                    )
                else:
                    ui_output = result.display or result.output
                await notify_tool_text_output(host, ui_output, tid, tool_event_id, tool_node, display_policy, ok)

            llm_content = maybe_persist_tool_result(
                result.output, tool_event_id, tid,
                session_id=host._session.id if host._session else "default",
                workspace=ctx.workspace,
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
        loop_controller = getattr(thread_state.turn_context, "loop_controller", None)

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
                commit_skip_reason = (
                    "was skipped after loop commit"
                    if _loop_iteration_committed(loop_controller)
                    else _goal_phase_committed(thread_state.turn_context)
                )
                if commit_skip_reason is not None:
                    executed.extend(
                        _infrastructure_skipped_tool(tc, reason=commit_skip_reason)
                        for tc in pending
                    )
                    state_update["should_continue"] = False
                    break
                if any(item.terminal_reason is not None for item in segment_executed):
                    executed.extend(
                        _infrastructure_skipped_tool(tc, reason="was skipped")
                        for tc in pending
                    )
                    break
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
            commit_skip_reason = (
                "was skipped after loop commit"
                if _loop_iteration_committed(loop_controller)
                else _goal_phase_committed(thread_state.turn_context)
            )
            if commit_skip_reason is not None:
                executed.extend(
                    _infrastructure_skipped_tool(tc, reason=commit_skip_reason)
                    for tc in suffix
                )
                state_update["should_continue"] = False
                break
            if any(item.terminal_reason is not None for item in segment_executed):
                executed.extend(
                    _infrastructure_skipped_tool(tc, reason="was skipped")
                    for tc in suffix
                )
                break
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

        terminal_reason = next(
            (
                item.terminal_reason
                for item in executed
                if item.terminal_reason is not None
            ),
            None,
        )
        if host._ui.via_events() and terminal_reason != UI_EVENT_BUS_TIMEOUT_KIND:
            try:
                await host._ui.events.drain()
            except Exception as exc:
                if hasattr(host._ui.events, "clear_error"):
                    host._ui.events.clear_error()
                log_tool_event(
                    "ui_event_drain_failed",
                    tool_name="ui_event_bus",
                    message=f"{type(exc).__name__}: {exc}",
                    session_id=host._session.id if host._session else None,
                )

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
        loop_terminal_message = None
        if state_update.get("should_continue") is False:
            loop_terminal_message = _loop_commit_terminal_message(loop_controller, last)
        if terminal_reason == UI_EVENT_BUS_TIMEOUT_KIND:
            tool_messages.append(AIMessage(content=(
                "Turn terminated: UI event bus timed out while notifying tool start. "
                "This usually indicates the frontend is unresponsive. "
                "The session is still alive — you can continue interacting."
            )))
            state_update["should_continue"] = False
            loop_terminal_message = None
        if loop_terminal_message is not None:
            await _publish_loop_terminal_message(host, loop_terminal_message)
            tool_messages.append(loop_terminal_message)
        if no_progress_decision.action == "terminate":
            tool_messages.append(AIMessage(content=no_progress_decision.message))
            state_update["should_continue"] = False
        return await _apply_pending_turn_stop_commit(
            host,
            {
                "messages": tool_messages,
                **state_update,
            },
        )

    tool_result_ok = staticmethod(_tool_result_ok)



def _approved_tool_risks_for_call(tool_call: dict) -> list[ApprovedToolRisk]:
    raw = (tool_call.get("metadata") or {}).get("approved_risk")
    if not isinstance(raw, dict):
        return []
    try:
        return [ApprovedToolRisk.model_validate(raw)]
    except ValueError:
        return []

async def _emit_tool_heartbeat(
    host: Any,
    *,
    tid: str,
    tool_event_id: str,
    started_at: float,
) -> None:
    await asyncio.sleep(TOOL_HEARTBEAT_INITIAL_SECONDS)
    while True:
        elapsed = time.monotonic() - started_at
        await host._ui.events.emit(StatusUpdated(
            status_id=f"tool-heartbeat:{tool_event_id}",
            label="Tool running",
            detail=f"{tid} still running ({elapsed:.0f}s elapsed)",
            stage="working",
            display="record_only",
            parent_tool_call_id=tool_event_id,
        ))
        await asyncio.sleep(TOOL_HEARTBEAT_INTERVAL_SECONDS)
