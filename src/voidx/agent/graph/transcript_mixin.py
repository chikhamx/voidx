"""Transcript snapshot persistence proxies for the agent graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.agent.graph.session_mixin import _session_runtime_for

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


class GraphTranscriptMixin:
    async def _persist_transcript_snapshot(self: GraphRunLoopHost) -> None:
        await _session_runtime_for(self).persist_transcript_snapshot()

    async def _restore_transcript_snapshot(self: GraphRunLoopHost, *, append: bool = False) -> bool:
        return await _session_runtime_for(self).restore_transcript_snapshot(append=append)
