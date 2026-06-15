"""Shared helpers for TUI modules — ANSI escape constants, pure functions."""

from __future__ import annotations

import re

from rich.cells import cell_len
from rich.markup import escape as rich_escape
from rich.text import Text

from voidx.ui.output.dock.formatting import ANSI_LINE_PREFIX
from voidx.ui.tools.file_picker import FileCandidate, format_size

_ANSI_STRIP_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

_DISABLE_MOUSE_REPORTING = (
    "\x1b[?9l"
    "\x1b[?1000l"
    "\x1b[?1002l"
    "\x1b[?1003l"
    "\x1b[?1005l"
    "\x1b[?1006l"
    "\x1b[?1015l"
)

_ENTER_TERMINAL_SEQUENCE = f"{_DISABLE_MOUSE_REPORTING}\x1b[?2004h\x1b[?25l"
_EXIT_TERMINAL_SEQUENCE = f"{_DISABLE_MOUSE_REPORTING}\x1b[?2004l\x1b[?25h"


def _ctrl(key: str) -> bytes:
    return bytes([ord(key) - 0x60])


def _is_printable(data: bytes) -> bool:
    """Single-byte printable character (including space)."""
    return len(data) == 1 and 0x20 <= data[0] <= 0x7E


def _is_utf8_continuation(b: int) -> bool:
    return 0x80 <= b <= 0xBF


def _utf8_len(first: int) -> int:
    if first < 0x80:
        return 1
    if first < 0xE0:
        return 2
    if first < 0xF0:
        return 3
    return 4


def _csi_modifier_has_shift(mod: int) -> bool:
    # xterm/CSI-u modifier values are 1 + bitmask, so Shift alone is 2.
    return mod > 1 and bool((mod - 1) & 1)


def _parse_csi_modifier(params: list[str]) -> int:
    try:
        return int(params[1]) if len(params) > 1 and params[1] else 0
    except ValueError:
        return 0


def _escape_markup(text: str) -> str:
    return rich_escape(text)


def _safe_status_value(value: object, fallback: str) -> str:
    try:
        text = str(value)
    except Exception:
        return fallback
    return text if text else fallback


def _call_status(func: object, fallback: str) -> str:
    if not callable(func):
        return fallback
    try:
        return _safe_status_value(func(), fallback)
    except Exception:
        return fallback


def _call_bool(func: object) -> bool:
    if not callable(func):
        return False
    try:
        return bool(func())
    except Exception:
        return False


def _call_int(func: object, fallback: int) -> int:
    if not callable(func):
        return fallback
    try:
        return int(func())
    except Exception:
        return fallback


def _rendered_row_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + 1


def _clip_cells(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    limit = width - cell_len("…")
    used = 0
    result: list[str] = []
    for char in text:
        char_width = cell_len(char)
        if used + char_width > limit:
            break
        result.append(char)
        used += char_width
    return "".join(result) + "…"


def _candidate_meta(candidate: FileCandidate) -> str:
    if candidate.kind == "dir":
        return f"  dir · {candidate.size} items"
    if candidate.kind == "image":
        return f"  image · {format_size(candidate.size)}"
    return f"  file · {format_size(candidate.size)}"


def _plain_line(line: str) -> str:
    """Convert a tree render line (Rich markup + possible ANSI) to plain text."""
    marker = line.find(ANSI_LINE_PREFIX)
    if marker == -1:
        return Text.from_markup(line).plain
    text = Text.from_markup(line[:marker])
    text.append_text(Text.from_ansi(line[marker + len(ANSI_LINE_PREFIX):]))
    return text.plain
