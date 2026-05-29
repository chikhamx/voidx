"""Formatting helpers for prompt_toolkit TUI rendering."""

from __future__ import annotations

import re
from io import StringIO
from typing import Any

from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from rich.console import Console
from rich.text import Text

from voidx.ui.dock_parts.formatting import ANSI_LINE_PREFIX

_ANSI_CONSOLE: Console | None = None


def _get_ansi_console(width: int) -> Console:
    global _ANSI_CONSOLE
    if _ANSI_CONSOLE is None or _ANSI_CONSOLE.width != width:
        _ANSI_CONSOLE = Console(
            file=StringIO(),
            force_terminal=True,
            color_system="truecolor",
            width=width,
        )
    return _ANSI_CONSOLE


def _rich_to_ansi(markup: str, width: int) -> str:
    console = _get_ansi_console(width)
    buffer = console.file
    buffer.seek(0)
    buffer.truncate()
    console.print(markup, end="")
    return buffer.getvalue()


def _lines_to_formatted_text(lines: list[str], width: int, *, follow_tail: bool = True) -> FormattedText:
    console = _get_ansi_console(width)
    result = []

    for line in lines:
        marker = line.find(ANSI_LINE_PREFIX)
        if marker == -1:
            markup_part = line
            ansi_part = ""
        else:
            markup_part = line[:marker]
            ansi_part = line[marker + len(ANSI_LINE_PREFIX):]

        if markup_part:
            try:
                text = Text.from_markup(markup_part)
                segments = console.render(text)
                for segment in segments:
                    style = segment.style
                    pt_style = ""
                    if style:
                        if style.color:
                            if style.color.is_system_defined:
                                pt_style += f"fg:{style.color.name} "
                            else:
                                pt_style += f"fg:{style.color.get_truecolor().hex} "
                        if style.bgcolor:
                            if style.bgcolor.is_system_defined:
                                pt_style += f"bg:{style.bgcolor.name} "
                            else:
                                pt_style += f"bg:{style.bgcolor.get_truecolor().hex} "
                        if style.bold:
                            pt_style += "bold "
                        if style.italic:
                            pt_style += "italic "
                        if style.underline:
                            pt_style += "underline "
                        if style.dim:
                            pt_style += "class:dim "
                    result.append((pt_style.strip(), segment.text))
            except Exception:
                result.append(("", markup_part))

        if ansi_part:
            from prompt_toolkit.formatted_text.ansi import ANSI

            parsed = ANSI(ansi_part + "\x1b[0m")
            result.extend(to_formatted_text(parsed))

        result.append(("", "\n"))

    if result and result[-1] == ("", "\n"):
        result.pop()
    if result and follow_tail:
        result.append(("[SetCursorPosition]", ""))

    return FormattedText(result)


def _continuation_prefix(line: str) -> str:
    text = _visible_text(line)
    leading = len(text) - len(text.lstrip(" "))
    stripped = text[leading:]
    extra = 0
    if stripped.startswith(("• ", "- ", "* ")):
        extra = 2
    else:
        ordered = re.match(r"\d+[.)]\s+", stripped)
        if ordered:
            extra = len(ordered.group(0))
        elif stripped.startswith(("├─ ", "└─ ")):
            extra = 3
    return " " * (leading + extra)


def _visible_text(line: str) -> str:
    marker = line.find(ANSI_LINE_PREFIX)
    if marker == -1:
        markup_part = line
        ansi_part = ""
    else:
        markup_part = line[:marker]
        ansi_part = line[marker + len(ANSI_LINE_PREFIX):]

    parts: list[str] = []
    if markup_part:
        try:
            parts.append(Text.from_markup(markup_part).plain)
        except Exception:
            parts.append(markup_part)
    if ansi_part:
        parts.append(Text.from_ansi(ansi_part).plain)
    return "".join(parts)


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"
    return text[: width - 1].rstrip() + "…"


def _friendly_choice_label(label: str, value: str, desc: str) -> str:
    if value == "a":
        return "Yes, and don't ask again this session"
    if value == "y":
        return "Yes, allow once"
    if value == "n":
        return "No, deny"
    return desc or label


def _permission_target(args: dict[str, Any]) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "query", "subagent_type"):
        value = args.get(key)
        if value:
            return str(value).replace("\n", " ")
    return ""


def _args_preview(args: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in args.items():
        if key in {"command", "file_path", "path", "pattern", "url", "query", "subagent_type"}:
            continue
        text = str(value).replace("\n", " ")
        parts.append(f"{key}={text}")
        if len(parts) >= 3:
            break
    return ", ".join(parts)


def _mcp_status_label(status: str) -> tuple[str, str]:
    normalized = status.strip().lower()
    if normalized in {"connected", "configured", "ready"}:
        label = "connected" if normalized == "connected" else normalized
        return ("class:command.ok", f"✓ {label}")
    if normalized in {"disabled", "off"}:
        return ("class:command.dim", "disabled")
    if normalized in {"error", "failed"}:
        return ("class:command.error", "✗ error")
    return ("class:command.dim", normalized or "unknown")
