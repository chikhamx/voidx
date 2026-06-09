"""Tool execution node for the agent graph."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, ToolMessage

from voidx.diffing import diff_stat
from voidx.agent.graph.runtime import current_parent_tool_call_id, ui
from voidx.agent.graph.todo_events import todo_updated_event
from voidx.agent.task_state import ToolStatePatch
from voidx.agent.tool_messages import sanitize_tool_message_content
from voidx.skills.runtime import SkillRunState
from voidx.tools.base import ToolContext, UserInteraction, UserResponse
from voidx.runtime.ui import (
    FileChangeAppended,
    StatusFinished,
    StatusUpdated,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    _fmt_args,
    _title,
    dock,
    session_tracker,
    ui_events,
    via_events,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphToolExecutionHost


_OTHER_VALUE_PREFIX = "__voidx_choice_prompt_other__"
AGENT_RESULT_PREVIEW_LINES = 5
AGENT_RESULT_PREVIEW_CHARS = 1200


@dataclass
class _ExecutedTool:
    message: ToolMessage
    result: object
    tool_call: dict


class GraphToolExecutionMixin:
    async def _execute_tools(self: GraphToolExecutionHost, state) -> dict:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {}

        if dock.active and dock.current_agent is not None:
            self._turn_node = dock.current_agent

        self._current_messages = state["messages"]
        agent_name = state.get("agent", "orchestrator")
        session_id = self._session.id if self._session else "default"
        plan_mode = state.get("plan_mode", False)
        interaction_mode = state.get("interaction_mode")
        ctx = ToolContext(
            workspace=state.get("workspace", self._workspace),
            session_id=session_id,
            agent=agent_name,
            interaction_mode=interaction_mode or ("plan" if plan_mode else "auto"),
            task_intent=state.get("task_intent", "chat"),
            pending_approval=_dump_pending_approval(state.get("pending_approval")),
            goal=state.get("goal", ""),
            goal_turn_count=state.get("goal_turn_count", 0),
            active_skill_names=_active_skill_names(state.get("skill_runs", []) or []),
            file_mtimes=self._file_mtimes,
            mcp_manager=getattr(self, "_mcp_manager", None),
            lsp_manager=getattr(self, "_lsp_manager", None),
            sandbox_mode=self._permission.sandbox_mode,
            sandbox_extra_paths=self._permission.sandbox_workspace_write,
            interact=_make_interact_callback(getattr(self, "_app", None)),
        )

        tool_calls = last.tool_calls
        self._sub_buffers = {}

        approved, denied = await self._authorize_tool_calls(
            tool_calls,
            agent_name=agent_name,
            plan_mode=plan_mode,
            session_id=session_id,
            interaction_mode=interaction_mode,
        )
        barrier_present = any(_is_barrier_tool(tc) for tc in approved)
        deferred_for_barrier: list[dict] = []
        if barrier_present:
            barrier = [tc for tc in approved if _is_barrier_tool(tc)]
            deferred_for_barrier = [tc for tc in approved if not _is_barrier_tool(tc)]
            approved = barrier

        # ── Phase 2: parallel execution of all approved tools ────────

        async def execute_one(tc):
            tid = tc["name"]
            targs = tc.get("args", {})
            cid = tc.get("id", "")

            tool_event_id = cid or f"{tid}:{id(tc)}"
            tool_node = None
            if via_events():
                gerund = _title(ui._TOOL_GERUND.get(tid, tid + "ing"))
                tool_node = await ui_events.request(ToolStarted(
                    tool_call_id=tool_event_id,
                    tool_name=tid,
                    label=gerund,
                    args=_fmt_args(targs),
                    raw_args=targs,
                ))
                if dock.active and dock.current_agent is not None:
                    self._turn_node = dock.current_agent
            elif dock.active:
                gerund = _title(ui._TOOL_GERUND.get(tid, tid + "ing"))
                tool_node = dock.start_tool(
                    gerund,
                    _fmt_args(targs),
                    tool_call_id=tool_event_id,
                    tool_name=tid,
                    raw_args=targs,
                )
                if dock.current_agent is not None:
                    self._turn_node = dock.current_agent
            else:
                ui.tool_call(tid, targs)

            t0 = time.monotonic()
            ok = True
            try:
                session_tracker.capture_tool_call(tid, targs, ctx.workspace, ctx.sandbox_extra_paths)
                parent_tool_token = current_parent_tool_call_id.set(tool_event_id)
                try:
                    result = await self.tools.execute_tool(tid, targs, ctx)
                finally:
                    current_parent_tool_call_id.reset(parent_tool_token)
                ok = self._tool_result_ok(result)
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
                failure_tc = self._needs_failure_check.get(cid, None)
                if failure_tc and self._permission.approval_policy == "on-failure":
                    self._notify_tool_failure(failure_tc, result)
            elif ok:
                self._clear_failure_check(cid)

            if via_events() and tid == "todo":
                todo_event = todo_updated_event(result)
                if todo_event is not None:
                    await ui_events.emit(todo_event)

            if via_events():
                await ui_events.emit(ToolFinished(
                    tool_call_id=tool_event_id,
                    label=_title(tid),
                    elapsed=elapsed,
                    ok=ok,
                ))
            elif tool_node:
                dock.finish_tool_node(tool_node, _title(tid), elapsed, ok)
            else:
                ui.tool_done(tid, elapsed, ok)

            # Render diff to terminal (if any)
            if getattr(result, "diff", None) and ok:
                session_tracker.record_diff(result.diff)
                if via_events():
                    await ui_events.emit(FileChangeAppended(
                        tool_call_id=tool_event_id,
                        diff_text=result.diff,
                    ))
                elif tool_node:
                    dock.append_file_change(
                        result.diff,
                        parent=tool_node,
                        tool_call_id=tool_event_id,
                    )
                else:
                    added, removed = diff_stat(result.diff)
                    ui.print(f"  [green]+{added}[/green] [red]−{removed}[/red]")
                if self._debug and not tool_node:
                    ui.diff(result.diff)
            elif self._debug or tid == "agent":
                output = _agent_result_preview(result.output) if tid == "agent" else result.output
                if via_events():
                    await ui_events.emit(ToolResultAppended(
                        tool_call_id=tool_event_id,
                        text=output,
                    ))
                elif tool_node:
                    dock.append_tool_result(
                        output,
                        parent=tool_node,
                        tool_call_id=tool_event_id,
                    )
                else:
                    ui.tool_result(output)

            return _ExecutedTool(
                message=ToolMessage(
                    content=sanitize_tool_message_content(result.output, workspace=ctx.workspace),
                    tool_call_id=cid,
                ),
                result=result,
                tool_call=tc,
            )

        agent_limit = _parallel_subagent_limit(self.config)
        agent_semaphore = asyncio.Semaphore(agent_limit)
        parallel_agent_count = sum(1 for tc in approved if tc.get("name") == "agent")
        aggregate_status_id = ""
        show_parallel_status = (
            agent_limit > 1
            and parallel_agent_count > 1
            and not barrier_present
        )

        async def execute_one_limited(tc):
            if tc.get("name") == "agent":
                async with agent_semaphore:
                    return await execute_one(tc)
            return await execute_one(tc)

        if show_parallel_status and via_events():
            aggregate_status_id = f"parallel-subagents:{id(last)}"
            await ui_events.emit(StatusUpdated(
                status_id=aggregate_status_id,
                label=f"Running {parallel_agent_count} child agents",
                stage="working",
            ))

        if barrier_present:
            executed = []
            for tc in approved:
                executed.append(await execute_one(tc))
        else:
            try:
                executed = await asyncio.gather(*[execute_one_limited(tc) for tc in approved])
            finally:
                if aggregate_status_id:
                    await ui_events.emit(StatusFinished(
                        status_id=aggregate_status_id,
                        label=f"Finished {parallel_agent_count} child agents",
                    ))

        # Clear on-failure tracking for this batch (full logic in Phase 2)
        self._needs_failure_check.clear()

        if via_events():
            await ui_events.drain()

        # Child-agent messages are buffered by parent tool_call_id. Append them
        # only after parent ToolMessages so parent tool_use→tool_result
        # adjacency is preserved for ALL agent calls.
        sub_buffers: dict[str, list] = getattr(self, "_sub_buffers", {})
        approved_ids = [tc.get("id", "") for tc in approved]
        extra: list = []
        for call_id in approved_ids:
            extra.extend(sub_buffers.get(call_id, []))
        for call_id, messages in sub_buffers.items():
            if call_id not in approved_ids:
                extra.extend(messages)
        self._sub_buffers = {}

        # Denied tools get error messages
        denied_msgs = [
            ToolMessage(
                content=sanitize_tool_message_content(reason, workspace=ctx.workspace),
                tool_call_id=tc.get("id", ""),
            )
            for tc, reason in denied
        ]
        deferred_msgs = [_deferred_message(tc, ctx.workspace) for tc in deferred_for_barrier]

        state_update = _state_update_from_executed_tools(
            executed,
            current_skill_runs=state.get("skill_runs", []) or [],
        )
        return {
            "messages": [item.message for item in executed] + extra + denied_msgs + deferred_msgs,
            **state_update,
        }

    @staticmethod
    def _tool_result_ok(result) -> bool:
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
    current_skill_runs: object = (),
) -> dict:
    update: dict = {}
    merged_skill_runs = _merge_skill_runs_for_state(current_skill_runs)
    skill_runs_changed = False
    for item in executed:
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
            elif field == "skill_runs":
                merged_skill_runs = _merge_skill_runs_for_state(
                    merged_skill_runs,
                    patch.skill_runs,
                )
                skill_runs_changed = True
            else:
                update[field] = data.get(field)
    if skill_runs_changed:
        update["skill_runs"] = merged_skill_runs
    return update


def _merge_skill_runs_for_state(*groups: object) -> list[SkillRunState]:
    merged: dict[str, SkillRunState] = {}
    for group in groups:
        items = group.values() if isinstance(group, dict) else group or []
        for item in items:
            try:
                run = item if isinstance(item, SkillRunState) else SkillRunState.model_validate(item)
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
    return tool_call.get("name") in {"on_intent", "clarify", "plan_checkpoint"}


def _deferred_message(tool_call: dict, workspace: str) -> ToolMessage:
    return ToolMessage(
        content=sanitize_tool_message_content(
            "Deferred until after a runtime barrier tool updates state. "
            "Re-issue this tool call if it is still allowed for the updated intent.",
            workspace=workspace,
        ),
        tool_call_id=tool_call.get("id", ""),
    )


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


def _active_skill_names(value: object) -> list[str]:
    names: list[str] = []
    items = value.values() if isinstance(value, dict) else value or []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = getattr(item, "name", None)
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names
