"""Shared console formatting helpers."""

from __future__ import annotations

import time
from contextvars import ContextVar
from io import StringIO

from rich.console import Console

from voidx.ui.output.agent_display import agent_display_name

_SPIN_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_spin_idx: ContextVar[int] = ContextVar("spin_idx", default=0)
_ORANGE = "#EBCB8B"  # Nord yellow-ish


def _next_spin() -> str:
    idx = _spin_idx.get()
    nxt = (idx + 1) % len(_SPIN_FRAMES)
    _spin_idx.set(nxt)
    return f"[{_ORANGE}]{_SPIN_FRAMES[nxt]}[/]"


def _done_spin() -> str:
    return "[#A3BE8C]●[/#A3BE8C]"


def _title(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _escape_rich(s: str) -> str:
    return s.replace("[", "\\[").replace("]", "\\]")


def _capture_ansi(width: int, render) -> str:
    buffer = StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        color_system="truecolor",
        width=width or 80,
    )
    render(console)
    return buffer.getvalue().rstrip("\n")


def _event_tool_id(tool_name: str) -> str:
    return f"console:{tool_name}:{time.time_ns()}"


def _pop_event_tool_id(pending: dict[str, list[str]], tool_name: str) -> str:
    ids = pending.get(tool_name, [])
    event_id = ids.pop(0) if ids else ""
    if not ids:
        pending.pop(tool_name, None)
    return event_id


def _fmt_args(args: dict[str, object]) -> str:
    """Format tool args Claude Code style: key="value" inside parentheses."""
    parts = []
    for k, v in args.items():
        if v is None or v == "":
            continue
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        escaped = _escape_rich(s)
        if k == "args":
            parts.append(f'"[cyan]{escaped}[/cyan]"')
        elif isinstance(v, str):
            parts.append(f'{k}="[cyan]{escaped}[/cyan]"')
        else:
            parts.append(f"{k}=[cyan]{escaped}[/cyan]")
    return ", ".join(parts)


def _fmt_args_short(tool_name: str, args: dict[str, object]) -> str:
    if tool_name in {"read", "file", "write", "replace"}:
        value = args.get("file_path")
        return _escape_rich(str(value)) if value else ""
    if tool_name == "glob":
        value = args.get("pattern")
        return _escape_rich(str(value)) if value else ""
    if tool_name == "grep":
        value = args.get("pattern")
        include = args.get("include")
        suffix = f" in {_escape_rich(str(include))}" if include else ""
        return f"{_escape_rich(str(value))}{suffix}" if value else ""
    if tool_name in ("bash", "powershell"):
        value = str(args.get("command", ""))
        shortened = value[:77] + "..." if len(value) > 80 else value
        return _escape_rich(shortened)
    if tool_name == "agent":
        return _escape_rich(agent_display_name(args.get("agent")))
    if tool_name in {"webfetch", "websearch"}:
        value = args.get("url") or args.get("query")
        return _escape_rich(str(value)) if value else ""
    return ""


fmt_args = _fmt_args
