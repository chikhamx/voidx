"""Tool execution node for the agent graph."""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import AIMessage, ToolMessage

from voidx.agent.graph_parts.runtime import current_parent_tool_call_id, ui
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


class GraphToolExecutionMixin:
    async def _execute_tools(self, state) -> dict:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {}

        if dock.current_agent is not None:
            self._turn_node = dock.current_agent

        self._current_messages = state["messages"]
        ctx = ToolContext(workspace=state.get("workspace", self._workspace), file_mtimes=self._file_mtimes)
        agent_name = state.get("agent", "orchestrator")
        session_id = self._session.id if self._session else "default"
        plan_mode = state.get("plan_mode", False)

        tool_calls = last.tool_calls

        approved, denied = await self._authorize_tool_calls(
            tool_calls,
            agent_name=agent_name,
            plan_mode=plan_mode,
            session_id=session_id,
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
                ))
                if dock.current_agent is not None:
                    self._turn_node = dock.current_agent
            elif dock.active:
                gerund = _title(ui._TOOL_GERUND.get(tid, tid + "ing"))
                tool_node = dock.start_tool(gerund, _fmt_args(targs))
                if dock.current_agent is not None:
                    self._turn_node = dock.current_agent
            else:
                ui.tool_call(tid, targs)

            t0 = time.monotonic()
            ok = True
            try:
                parent_tool_token = current_parent_tool_call_id.set(tool_event_id)
                try:
                    result = await self.tools.execute_tool(tid, targs, ctx)
                finally:
                    current_parent_tool_call_id.reset(parent_tool_token)
            except Exception as e:
                from voidx.tools.base import ToolResult
                result = ToolResult(
                    output=f"Tool execution error: {e}",
                    metadata={"error": str(e)},
                )
                ok = False
            elapsed = time.monotonic() - t0
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
                if dock.active and ui_events.is_running:
                    await ui_events.emit(FileChangeAppended(
                        tool_call_id=tool_event_id,
                        diff_text=result.diff,
                    ))
                elif tool_node:
                    dock.append_file_change(result.diff, parent=tool_node)
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
                    dock.append_tool_result(result.output, parent=tool_node)
                else:
                    ui.tool_result(result.output)

            return ToolMessage(content=result.output, tool_call_id=cid)

        # Run all approved tools in parallel
        executed = await asyncio.gather(*[execute_one(tc) for tc in approved])
        if dock.active and ui_events.is_running:
            await ui_events.drain()

        # Sub-agent messages are buffered in self._sub_buffer (never mutated
        # state["messages"] directly). Append them after ToolMessages so
        # tool_use→tool_result adjacency is preserved for ALL tool calls.
        extra: list = list(getattr(self, '_sub_buffer', []))
        self._sub_buffer = []

        # Denied tools get error messages
        denied_msgs = [
            ToolMessage(content=reason, tool_call_id=tc.get("id", ""))
            for tc, reason in denied
        ]

        return {"messages": list(executed) + extra + denied_msgs}
