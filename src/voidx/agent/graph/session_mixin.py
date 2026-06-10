"""Runtime session state persistence proxies for the agent graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.agent.graph.session_runtime import GraphSessionRuntime

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


def _session_runtime_for(host: GraphRunLoopHost) -> GraphSessionRuntime:
    runtime = getattr(host, "_session_runtime", None)
    if runtime is None:
        # Bare mixin tests can instantiate a host without VoidXGraph.__init__.
        runtime = GraphSessionRuntime(host)
        host._session_runtime = runtime
    return runtime


class GraphSessionMixin:
    def _session_component(self: GraphRunLoopHost) -> GraphSessionRuntime:
        return _session_runtime_for(self)

    def _reset_runtime_state_memory(self: GraphRunLoopHost) -> None:
        _session_runtime_for(self).reset_runtime_state_memory()

    async def _restore_runtime_state(self: GraphRunLoopHost) -> None:
        await _session_runtime_for(self).restore_runtime_state()

    async def _persist_runtime_state(self: GraphRunLoopHost) -> None:
        await _session_runtime_for(self).persist_runtime_state()

    async def _clear_runtime_state(self: GraphRunLoopHost) -> None:
        await _session_runtime_for(self).clear_runtime_state(
            reset_runtime_state_memory=self._reset_runtime_state_memory,
        )
