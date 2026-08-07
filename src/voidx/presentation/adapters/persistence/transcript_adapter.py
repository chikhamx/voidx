"""Presentation-owned transcript snapshot adapter."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.presentation.adapters.persistence.transcript_snapshot import (
    append_transcript_reset,
    load_transcript,
    replace_transcript,
    transcript_rows_to_tree,
    tree_to_transcript_rows,
)


class TranscriptDock(Protocol):
    tree: Any

    def restore_tree(self, tree: Any, *, append: bool = False) -> None: ...


class TranscriptUi(Protocol):
    def get_dock(self) -> TranscriptDock | None: ...


class TranscriptSnapshotAdapter:
    """Persist and restore the active presentation tree as transcript JSONL."""

    def __init__(self, ui: TranscriptUi) -> None:
        self._ui = ui

    async def persist_current(self, session_id: str) -> None:
        active_dock = self._ui.get_dock()
        if active_dock is None:
            return
        rows, turn_count = tree_to_transcript_rows(session_id, active_dock.tree)
        await replace_transcript(session_id, rows, turn_count=turn_count)

    async def restore_current(self, session_id: str, *, append: bool = False) -> bool:
        active_dock = self._ui.get_dock()
        if active_dock is None:
            return False
        rows = await load_transcript(session_id)
        if not rows:
            return False
        active_dock.restore_tree(transcript_rows_to_tree(rows), append=append)
        return True

    async def clear(self, session_id: str) -> None:
        await append_transcript_reset(session_id, reason="clear_messages")
