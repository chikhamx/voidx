"""Shared file state helpers for write-like tools."""

from __future__ import annotations

from pathlib import Path

from voidx.tools.base import ToolContext


def check_staleness(ctx: ToolContext, resolved: Path) -> str | None:
    key = str(resolved.resolve())
    if key not in ctx.file_mtimes:
        return None
    if not resolved.exists():
        return f"File deleted since last read: {resolved}"
    current_mtime = resolved.stat().st_mtime
    if current_mtime != ctx.file_mtimes[key]:
        return (
            f"File was modified since last read: {resolved}. "
            "Please re-read the file before editing."
        )
    return None


def record_mtime(ctx: ToolContext, resolved: Path) -> None:
    if resolved.exists():
        ctx.file_mtimes[str(resolved.resolve())] = resolved.stat().st_mtime

