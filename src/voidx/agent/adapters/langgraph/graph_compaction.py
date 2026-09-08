"""Adapter exposing the existing graph compaction algorithm as a small port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from voidx.agent.domain.compaction import CompactionResult
from voidx.llm.compaction import estimate_context_tokens


class GraphCompactionAdapter:
    def __init__(
        self,
        coordinator,
        *,
        run_compaction_agent: Callable[[list, str | None], Awaitable[str | None]] | None = None,
        persist_compaction: Callable[[list], Awaitable[None]] | None = None,
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
        if not force:
            host = self._coordinator.host
            total_tokens = estimate_context_tokens(messages, host.config.model.model)
            tokens = {"total": total_tokens, "input": total_tokens, "output": 0, "reasoning": 0}
            over_hard = host._compaction.is_overflow(tokens)
            over_soft = preflight and host._compaction.is_soft_overflow(tokens)
            if not over_hard and not over_soft:
                return None
            if ask and getattr(host.config, "ask_compact", False):
                if not await self._coordinator.ask_compact(total_tokens):
                    return None
        return await self._coordinator.rollover_for_live_state(
            messages,
            force=force,
            run_compaction_agent=self._run_compaction_agent,
        )
