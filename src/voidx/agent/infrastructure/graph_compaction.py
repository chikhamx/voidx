"""Adapter exposing the existing graph compaction algorithm as a small port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from voidx.agent.domain.compaction import CompactionResult


class GraphCompactionAdapter:
    def __init__(
        self,
        coordinator,
        *,
        run_compaction_agent: Callable[[list, str | None], Awaitable[str | None]],
        persist_compaction: Callable[[list], Awaitable[None]],
    ) -> None:
        self._coordinator = coordinator
        self._run_compaction_agent = run_compaction_agent
        self._persist_compaction = persist_compaction

    async def compact(
        self,
        messages: list,
        session_messages: list | None = None,
        *,
        force: bool = False,
        ask: bool = True,
        preflight: bool = False,
    ) -> CompactionResult | None:
        return await self._coordinator.compact_for_live_state(
            messages,
            session_messages,
            force=force,
            ask=ask,
            preflight=preflight,
            run_compaction_agent=self._run_compaction_agent,
            persist_compaction=self._persist_compaction,
        )
