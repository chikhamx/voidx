"""Transcript snapshot persistence for the agent graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.memory.transcript import load_transcript, replace_transcript
from voidx.runtime.ui import get_dock, transcript_rows_to_tree, tree_to_transcript_rows

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


class GraphTranscriptMixin:
    async def _persist_transcript_snapshot(self: GraphRunLoopHost) -> None:
        if self._session is None:
            return
        active_dock = get_dock()
        if active_dock is None:
            return
        rows, turn_count = tree_to_transcript_rows(self._session.id, active_dock.tree)
        await replace_transcript(self._session.id, rows, turn_count=turn_count)

    async def _restore_transcript_snapshot(self: GraphRunLoopHost, *, append: bool = False) -> bool:
        if self._session is None:
            return False
        active_dock = get_dock()
        if active_dock is None:
            return False
        rows = await load_transcript(self._session.id)
        if not rows:
            return False
        active_dock.restore_tree(transcript_rows_to_tree(rows), append=append)
        return True
