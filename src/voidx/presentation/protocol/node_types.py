"""Shared presentation node schema types."""

from __future__ import annotations

from typing import Literal


NodeType = Literal[
    "root",
    "startup",
    "turn",
    "tool_call",
    "tool_result",
    "todo",
    "subagent",
    "message",
    "assistant",
    "thought",
    "status",
    "permission",
    "checkpoint",
    "error",
    "warn",
    "diff",
]
Status = Literal["running", "done", "error"]
