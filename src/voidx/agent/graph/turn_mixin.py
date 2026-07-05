"""Compatibility proxies for single-turn agent graph execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from voidx.agent.graph.turn_runner import (
    RESUME_FORCE_COMPACT_MESSAGE_COUNT,
    GraphTurnRunner,
    _resolve_recursion_limit,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


def _turn_runner_for(host: GraphRunLoopHost) -> GraphTurnRunner:
    runner = getattr(host, "_turn_runner", None)
    if runner is None:
        # Bare mixin tests can instantiate a host without VoidXGraph.__init__.
        runner = GraphTurnRunner(host)
        host._turn_runner = runner
    return runner


class GraphTurnMixin:
    def _turn_runner_component(self: GraphRunLoopHost) -> GraphTurnRunner:
        return _turn_runner_for(self)

    async def _run_once(
        self: GraphRunLoopHost,
        user_text: str,
        *,
        display_text: str | None = None,
        context: Any | None = None,
    ) -> None:
        await _turn_runner_for(self).run_once(user_text, display_text=display_text, context=context)
