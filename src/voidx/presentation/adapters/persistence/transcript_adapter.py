"""Presentation-owned transcript snapshot adapter."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.presentation.adapters.persistence.transcript_snapshot import (
    append_transcript_reset,
    append_transcript_turns,
    complete_transcript_turn_ids,
    load_transcript,
    tree_to_transcript_turn_rows,
    tree_transcript_turn_ids,
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
        turn_ids = tree_transcript_turn_ids(active_dock.tree)
        if not turn_ids:
            return

        persisted_turn_ids = await complete_transcript_turn_ids(session_id)
        pending_turns: list[tuple[int, Any]] = []
        for turn_id in turn_ids:
            if turn_id in persisted_turn_ids:
                continue
            rows = tree_to_transcript_turn_rows(session_id, active_dock.tree, turn_id)
            if rows:
                pending_turns.append((turn_id, rows))
        if pending_turns:
            written_turn_ids = await append_transcript_turns(session_id, pending_turns)
            mark_durable = getattr(active_dock.tree, "mark_root_turn_durable", None)
            if callable(mark_durable):
                for turn_id in written_turn_ids:
                    mark_durable(turn_id)

    async def restore_current(self, session_id: str, *, append: bool = False) -> bool:
        active_dock = self._ui.get_dock()
        if active_dock is None:
            return False
        rows = await load_transcript(session_id)
        if not rows:
            return False
        restored_tree = transcript_rows_to_tree(rows)
        for turn_id in {row.turn_id for row in rows}:
            restored_tree.mark_root_turn_durable(turn_id)
        active_dock.restore_tree(restored_tree, append=append)
        return True

    async def clear(self, session_id: str) -> None:
        await append_transcript_reset(session_id, reason="clear_messages")
