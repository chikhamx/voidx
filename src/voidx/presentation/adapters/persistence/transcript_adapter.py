"""Presentation-owned transcript snapshot adapter."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.presentation.adapters.persistence.transcript_snapshot import (
    append_transcript_reset,
    append_transcript_turns,
    complete_transcript_turn_ids,
    load_transcript,
    tree_to_transcript_turn_rows,
    tree_turn_count,
    transcript_rows_to_tree,
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
        turn_count = tree_turn_count(active_dock.tree)
        if turn_count <= 0:
            return

        persisted_turn_ids = await complete_transcript_turn_ids(session_id)
        pending_turns: list[tuple[int, Any]] = []
        for turn_id in range(turn_count):
            if turn_id in persisted_turn_ids:
                continue
            rows = tree_to_transcript_turn_rows(session_id, active_dock.tree, turn_id)
            if rows:
                pending_turns.append((turn_id, rows))
        if pending_turns:
            await append_transcript_turns(session_id, pending_turns)

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
