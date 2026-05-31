"""Tool execution node for the agent graph."""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import AIMessage, ToolMessage

from voidx.agent.graph_components.runtime import current_parent_tool_call_id, ui
from voidx.tools.base import ToolContext
from voidx.ui.console import _fmt_args, _title
from voidx.ui.dock import dock
from voidx.ui.events import (
    FileChangeAppended,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    ui_events,
)
from voidx.ui.session_changes import session_tracker


class GraphToolExecutionMixin:
    async def _execute_tools(self, state) -> dict:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {}

        if dock.active and dock.current_agent is not None:
            self._turn_node = dock.current_agent

        self._current_messages = state["messages"]
        ctx = ToolContext(
            workspace=state.get("workspace", self._workspace),
            file_mtimes=self._file_mtimes,
            mcp_manager=getattr(self, "_mcp_manager", None),
            lsp_manager=getattr(self, "_lsp_manager", None),
            sandbox_extra_paths=self._permission.sandbox_workspace_write,
        )
        agent_name = state.get("agent", "orchestrator")
        session_id = self._session.id if self._session else "default"
        plan_mode = state.get("plan_mode", False)
        interaction_mode = state.get("interaction_mode")

        tool_calls = last.tool_calls
        self._sub_buffers = {}

        approved, denied = await self._authorize_tool_calls(
            tool_calls,
            agent_name=agent_name,
            plan_mode=plan_mode,
            session_id=session_id,
            interaction_mode=interaction_mode,
        )

        # ── Phase 2: parallel execution of all approved tools ────────

        async def execute_one(tc):
            tid = tc["name"]
            targs = tc.get("args", {})
            cid = tc.get("id", "")

            tool_event_id = cid or f"{tid}:{id(tc)}"
            tool_node = None
            if dock.active and ui_events.is_running:
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
                result = ToolResult(
                    output=f"Tool execution error: {e}",
                    metadata={"error": str(e)},
                )
                ok = False
            elapsed = time.monotonic() - t0

            # on-failure: notify user when auto-approved tool fails
            if not ok and hasattr(self, "_needs_failure_check"):
                failure_tc = self._needs_failure_check.get(cid, None)
                if failure_tc and self._permission.approval_policy == "on-failure":
                    self._notify_tool_failure(failure_tc, result)
            elif ok and hasattr(self, "_needs_failure_check"):
                self._clear_failure_check(cid)

            if dock.active and ui_events.is_running:
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
                if dock.active and ui_events.is_running:
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
                    from voidx.ui.diff import diff_stat
                    added, removed = diff_stat(result.diff)
                    ui.print(f"  [green]+{added}[/green] [red]−{removed}[/red]")
                if self._debug and not tool_node:
                    ui.diff(result.diff)
            elif self._debug:
                if dock.active and ui_events.is_running:
                    await ui_events.emit(ToolResultAppended(
                        tool_call_id=tool_event_id,
                        text=result.output,
                    ))
                elif tool_node:
                    dock.append_tool_result(
                        result.output,
                        parent=tool_node,
                        tool_call_id=tool_event_id,
                    )
                else:
                    ui.tool_result(result.output)

            return ToolMessage(content=result.output, tool_call_id=cid)

        # Run all approved tools in parallel
        executed = await asyncio.gather(*[execute_one(tc) for tc in approved])

        # Clear on-failure tracking for this batch (full logic in Phase 2)
        if hasattr(self, "_needs_failure_check"):
            self._needs_failure_check.clear()

        if dock.active and ui_events.is_running:
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
            ToolMessage(content=reason, tool_call_id=tc.get("id", ""))
            for tc, reason in denied
        ]

        return {"messages": list(executed) + extra + denied_msgs}

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
