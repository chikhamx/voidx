"""Presentation adapter for agent semantic messages."""

from __future__ import annotations

from typing import Any


class UiAgentEventPublisher:
    def __init__(self, ui_runtime: Any) -> None:
        self._ui = ui_runtime

    def publish_message(self, message: str) -> None:
        self._ui.ui.print(message)

    def start_turn(self, text: str) -> None:
        self._ui.dock.start_turn(text)

    def show_loop_waiting(self, wakeup_at: float) -> None:
        from voidx.presentation.output.events import StatusUpdated

        self._ui.events.emit_nowait(StatusUpdated(
            status_id="loop:waiting",
            label="Looping",
            detail=str(wakeup_at),
            display="record_only",
        ))

    def clear_loop_waiting(self) -> None:
        from voidx.presentation.output.events import StatusFinished

        self._ui.events.emit_nowait(StatusFinished(status_id="loop:waiting"))
