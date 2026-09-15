"""Ghostty trim text semantics: background-only rows are trimmable, text stops.

Pins the parity between `_visible_history_for_size` and Ghostty's
`Cell.hasTextAny`: a row filled with background color but no glyphs counts as
blank (trimmable); a row with any visible glyph stops the trim. The live cursor
row is NOT pinned (no saved-cursor/selection tracked pins in this TUI).
"""

import pytest
from types import SimpleNamespace

from tui_helpers import _tui, setup_dock  # noqa: F401


def _make(tui, lines, cursor_row=22):
    tui._visible_committed_rows = 14
    tui._prev_frame_lines = lines
    tui._applied_layout_snapshot = SimpleNamespace(
        terminal_width=80, terminal_height=24, frame_start_row=15, cursor_row=cursor_row,
    )


def test_background_only_rows_are_trimmable(tmp_path, monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    tui = _tui(tmp_path)
    # Frame rows: text at top, then background-colored blank rows (no glyphs).
    lines = ["\x1b[41mtext\x1b[0m"] + ["\x1b[44m" + " " * 80 + "\x1b[0m"] * 9
    _make(tui, lines, cursor_row=15)
    # Last text row = frame_start(15). retained_end=15. 24->20: shifted=0? no:
    # retained_end(15) - height(20) < 0 -> shifted 0. But the 9 blank tail rows
    # are trimmable, so the visible history should NOT be reduced.
    assert tui._visible_history_for_size(80, 20) == 14


def test_cursor_row_does_not_pin_when_blank(tmp_path, monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    tui = _tui(tmp_path)
    # Cursor sits on a blank row (no saved-cursor/selection pin in this TUI).
    lines = ["\x1b[41mtext\x1b[0m"] + [""] * 9
    _make(tui, lines, cursor_row=20)
    # Cursor on blank row 20 must NOT stop the trim: last text row = 15.
    assert tui._visible_history_for_size(80, 18) == 14


def test_text_row_stops_trim(tmp_path, monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    tui = _tui(tmp_path)
    lines = ["\x1b[41mtext\x1b[0m"] + [""] * 8 + ["\x1b[41mstatus\x1b[0m"]
    _make(tui, lines, cursor_row=20)
    # Last text row = frame_start(15)+9 = 24. 24->20: shifted = 24-20 = 4.
    assert tui._visible_history_for_size(80, 20) == 10
