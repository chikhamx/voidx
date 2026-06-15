"""Compatibility proxies for the agent graph tool execution node."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.agent.graph.tool_executor import (
    AGENT_RESULT_PREVIEW_CHARS,
    GraphToolExecutor,
    _agent_result_preview,
    _make_interact_callback,
    todo_updated_event,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphToolExecutionHost


def _tool_executor_for(host: GraphToolExecutionHost) -> GraphToolExecutor:
    executor = getattr(host, "_tool_executor", None)
    if executor is None:
        # Bare mixin tests can instantiate a host without VoidXGraph.__init__.
        executor = GraphToolExecutor(host)
        host._tool_executor = executor
    return executor


class GraphToolExecutionMixin:
    def _tool_execution_component(self: GraphToolExecutionHost) -> GraphToolExecutor:
        return _tool_executor_for(self)

    async def _execute_tools(self: GraphToolExecutionHost, state) -> dict:
        return await _tool_executor_for(self).execute_tools(
            state,
            tool_result_ok=self._tool_result_ok,
        )

    @staticmethod
    def _tool_result_ok(result) -> bool:
        return GraphToolExecutor.tool_result_ok(result)
