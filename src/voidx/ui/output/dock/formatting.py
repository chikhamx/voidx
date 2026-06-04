"""Formatting helpers for dock rendering."""

from __future__ import annotations

import re
from io import StringIO

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
_ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
ANSI_LINE_PREFIX = "\x00voidx-ansi\x00"


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text).rstrip("\n")


def _ansi_line(text: str) -> str:
    return ANSI_LINE_PREFIX + text


def _ansi_rgb(text: str, rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"\x1b[38;2;{r};{g};{b}m{text}\x1b[0m"


def _text_from_line(line: str) -> Text:
    marker = line.find(ANSI_LINE_PREFIX)
    if marker == -1:
        return Text.from_markup(line)
    text = Text.from_markup(line[:marker])
    text.append_text(Text.from_ansi(line[marker + len(ANSI_LINE_PREFIX):]))
    return text


def _markdown_lines(text: str, width: int) -> list[str]:
    buffer = StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        color_system="truecolor",
        width=width or 80,
        _environ={},
    )
    console.print(Markdown(text), end="")

    lines: list[str] = []
    for raw_line in buffer.getvalue().rstrip("\n").splitlines():
        stripped = _strip_ansi_trailing_space(raw_line)
        parts = stripped.splitlines() or [stripped]
        lines.extend(parts)

    return [line for line in lines if _clean(line).strip()]


def _strip_ansi_trailing_space(line: str) -> str:
    text = Text.from_ansi(_strip_ansi_backgrounds(line))
    text.rstrip()
    buffer = StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        color_system="truecolor",
        width=10_000,
        _environ={},
    )
    console.print(text, end="")
    return buffer.getvalue()


def _short_value(value: object) -> str:
    text = str(value).replace("\n", "\\n")
    return text[:157] + "..." if len(text) > 160 else text


def _short_path(path: str, limit: int = 96) -> str:
    if len(path) <= limit:
        return path
    keep = max((limit - 1) // 2, 1)
    return f"{path[:keep]}…{path[-keep:]}"


def _tail_lines(text: str, limit: int) -> list[str]:
    if not text.strip():
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def _strip_ansi_backgrounds(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        if raw == "":
            return match.group(0)
        parts = raw.split(";")
        kept: list[str] = []
        i = 0
        while i < len(parts):
            part = parts[i]
            if part == "48":
                mode = parts[i + 1] if i + 1 < len(parts) else ""
                if mode == "5":
                    i += 3
                elif mode == "2":
                    i += 5
                else:
                    i += 1
                continue
            try:
                value = int(part)
            except ValueError:
                kept.append(part)
                i += 1
                continue
            if 40 <= value <= 49 or 100 <= value <= 107:
                i += 1
                continue
            kept.append(part)
            i += 1
        return f"\x1b[{';'.join(kept)}m" if kept else ""

    return _ANSI_SGR_RE.sub(replace, text)
