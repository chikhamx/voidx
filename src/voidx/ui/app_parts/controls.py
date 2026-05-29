"""Prompt-toolkit controls used by the TUI."""

from __future__ import annotations

from typing import Callable

from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import Margin
from prompt_toolkit.mouse_events import MouseEvent


class TranscriptControl(FormattedTextControl):
    def __init__(self, tui) -> None:
        self._tui = tui
        super().__init__(tui._render_body, focusable=False, show_cursor=False)

    def mouse_handler(self, mouse_event: MouseEvent) -> None:
        return self._tui._handle_body_mouse(mouse_event)


class TranscriptScrollbarMargin(Margin):
    def __init__(self, tui) -> None:
        self._tui = tui

    def get_width(self, get_ui_content: Callable[[], object]) -> int:
        return 1

    def create_margin(self, window_render_info: object, width: int, height: int) -> list[tuple[str, str]]:
        return self._tui._render_scrollbar_margin(height)
