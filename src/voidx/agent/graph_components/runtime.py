"""Shared runtime state for agent orchestration."""

from __future__ import annotations

from contextvars import ContextVar

from voidx.ui.console import VoidConsole

ui = VoidConsole()
console = ui.console
current_parent_tool_call_id: ContextVar[str] = ContextVar(
    "current_parent_tool_call_id",
    default="",
)
