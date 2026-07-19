"""Context compaction engine port."""

from __future__ import annotations

from typing import Protocol

from voidx.agent.domain.compaction import CompactionResult


class CompactionEngine(Protocol):
    async def compact(
        self,
        messages: list,
        session_messages: list | None = None,
        *,
        force: bool = False,
        ask: bool = True,
        preflight: bool = False,
    ) -> CompactionResult | None: ...
