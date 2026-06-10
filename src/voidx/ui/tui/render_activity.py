"""Busy activity rendering helpers."""

from __future__ import annotations

import time

from rich.text import Text

from voidx.llm.usage import format_token_count
from voidx.ui.output.dock import active_agent_step_text
from voidx.ui.tui.activity import (
    BUSY_ACTIVITY_DEFAULT_VERB,
    BUSY_ACTIVITY_GLYPHS,
    BUSY_ACTIVITY_GLYPH_STYLES,
    BUSY_ACTIVITY_STYLE,
)
from voidx.ui.tui.helpers import _clip_cells


class _ActivityRendererMixin:
    def _busy_activity_row_count(self, width: int) -> int:
        return len(self._render_busy_activity_elements(width))

    def _render_busy_activity_elements(self, width: int) -> list[Text]:
        if not self._busy:
            return []
        return [self._busy_activity_text(width)]

    def _busy_activity_text(self, width: int) -> Text:
        label = _clip_cells(self._busy_activity_label(), width)
        if not label:
            return Text()
        text = Text()
        text.append(label[:1], style=self._busy_activity_glyph_style())
        if len(label) > 1:
            text.append(label[1:], style=BUSY_ACTIVITY_STYLE)
        return text

    def _busy_activity_glyph_style(self) -> str:
        return BUSY_ACTIVITY_GLYPH_STYLES[
            self._busy_activity_tick % len(BUSY_ACTIVITY_GLYPH_STYLES)
        ]

    def _busy_activity_label(self) -> str:
        started_at = self._busy_started_at
        glyph = BUSY_ACTIVITY_GLYPHS[
            self._busy_activity_tick % len(BUSY_ACTIVITY_GLYPHS)
        ]
        verb = self._busy_activity_verb or BUSY_ACTIVITY_DEFAULT_VERB
        if started_at is None:
            return f"{glyph} {verb}"
        elapsed = max(0, int(time.monotonic() - started_at))
        details = [self._format_elapsed(elapsed)]
        step = active_agent_step_text()
        if step:
            details.append(step)
        token_text = self._turn_token_text()
        if token_text:
            details.append(token_text)
        return f"{glyph} {verb} ({' '.join(details)})"

    def _turn_token_text(self) -> str:
        stats = getattr(self.status, "usage_stats", None)
        if stats is None:
            return ""
        turn_in = getattr(stats, "turn_input_tokens", 0)
        turn_out = getattr(stats, "turn_output_tokens", 0)
        if turn_in <= 0 and turn_out <= 0:
            return ""
        return f"↑{format_token_count(turn_in)} ↓{format_token_count(turn_out)}"

    @staticmethod
    def _format_elapsed(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        minutes, remainder = divmod(seconds, 60)
        return f"{minutes}m {remainder}s"
