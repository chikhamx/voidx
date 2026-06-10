"""Context compaction method proxies for the agent graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.agent.graph.compaction_coordinator import GraphCompactionCoordinator

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphCompactionHost


def _compaction_component_for(host: GraphCompactionHost) -> GraphCompactionCoordinator:
    coordinator = getattr(host, "_compaction_coordinator", None)
    if coordinator is None:
        # Bare mixin tests can instantiate a host without VoidXGraph.__init__.
        coordinator = GraphCompactionCoordinator(host)
        host._compaction_coordinator = coordinator
    return coordinator


class GraphCompactionMixin:
    def _compaction_component(self: GraphCompactionHost) -> GraphCompactionCoordinator:
        return _compaction_component_for(self)

    async def _maybe_compact(
        self: GraphCompactionHost,
        messages: list,
        session_msgs: list | None = None,
        *,
        force: bool = False,
        ask: bool = True,
    ) -> tuple[list | None, str | None]:
        return await _compaction_component_for(self).maybe_compact(
            messages,
            session_msgs,
            force=force,
            ask=ask,
            run_compaction_agent=self._run_compaction_agent,
            persist_compaction=self._persist_compaction,
        )

    async def _ask_compact(self: GraphCompactionHost, total_tokens: int) -> bool:
        return await _compaction_component_for(self).ask_compact(total_tokens)

    async def _persist_compaction(self: GraphCompactionHost, head_messages: list) -> None:
        await _compaction_component_for(self).persist_compaction(head_messages)

    async def _compact_session_history(self: GraphCompactionHost, *, force: bool = True) -> bool:
        return await _compaction_component_for(self).compact_session_history(
            force=force,
            run_compaction_agent=self._run_compaction_agent,
            persist_compaction=self._persist_compaction,
        )

    async def _run_compaction_agent(
        self: GraphCompactionHost,
        head_messages: list,
        previous_summary: str | None,
    ) -> str | None:
        return await _compaction_component_for(self).run_compaction_agent(
            head_messages,
            previous_summary,
        )
