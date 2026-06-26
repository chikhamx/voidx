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
_FORMAT_CONSOLES: dict[int, tuple[StringIO, Console]] = {}


def _format_console(width: int) -> tuple[StringIO, Console]:
    render_width = max(width or 80, 1)
    cached = _FORMAT_CONSOLES.get(render_width)
    if cached is None:
        buffer = StringIO()
        console = Console(
            file=buffer,
            force_terminal=True,
            color_system="truecolor",
            width=render_width,
            _environ={},
        )
        cached = (buffer, console)
        _FORMAT_CONSOLES[render_width] = cached
    else:
        buffer, _console = cached
        buffer.seek(0)
        buffer.truncate(0)
    return cached


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
    buffer, console = _format_console(width or 80)
    console.print(Markdown(text), end="")

    lines: list[str] = []
    for raw_line in buffer.getvalue().rstrip("\n").splitlines():
        stripped = _strip_ansi_trailing_space(raw_line)
        parts = stripped.splitlines() or [stripped]
        lines.extend(parts)

    # Preserve single blank lines (paragraph breaks) but collapse runs.
    # Drop lines that are pure ANSI decoration with no visible text.
    result: list[str] = []
    prev_blank = False
    for line in lines:
        is_blank = not line or not _clean(line).strip()
        if is_blank:
            if not prev_blank:
                result.append("")
            prev_blank = True
        else:
            result.append(line)
            prev_blank = False
    return result


def _strip_ansi_trailing_space(line: str) -> str:
    text = Text.from_ansi(_strip_ansi_backgrounds(line))
    text.rstrip()
    buffer, console = _format_console(10_000)
    console.print(text, end="")
    return buffer.getvalue()


def short_value(value: object) -> str:
    text = str(value).replace("\n", "\\n")
    return text[:157] + "..." if len(text) > 160 else text


def short_path(path: str, limit: int = 96) -> str:
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
