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

    def end_turn(self) -> None:
        from voidx.presentation.output.events import TurnCompleted

        self._ui.events.emitnowait(TurnCompleted())

    def cancel_turn(self) -> None:
        from voidx.presentation.output.events import TurnCancelled

        self._ui.events.emitnowait(TurnCancelled())

    def fail_turn(self, message: str) -> None:
        from voidx.presentation.output.events import TurnFailed

        self._ui.events.emitnowait(TurnFailed(message=message))

    def show_loop_waiting(self, wakeup_at: float) -> None:
        from voidx.presentation.output.events import StatusUpdated

        self._ui.events.emitnowait(StatusUpdated(
            status_id="loop:waiting",
            label="Looping",
            detail=str(wakeup_at),
            display="record_only",
        ))

    def clear_loop_waiting(self) -> None:
        from voidx.presentation.output.events import StatusFinished

        self._ui.events.emitnowait(StatusFinished(status_id="loop:waiting"))
