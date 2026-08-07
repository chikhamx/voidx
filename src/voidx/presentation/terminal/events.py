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
