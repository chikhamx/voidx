"""Tool execution result values and timeout metadata."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ToolResult(BaseModel):
    """Structured result returned by every tool plugin."""

    title: str = ""
    output: str
    summary: str = ""
    metadata: dict[str, Any] = {}
    diff: str | None = None
    next_step_hint: str = ""
    display: str = ""

    @classmethod
    def denied(cls, output: str) -> "ToolResult":
        return cls(output=output, metadata={"error": True, "denied": True})

    @classmethod
    def unavailable(cls, output: str) -> "ToolResult":
        return cls(output=output, metadata={"error": True, "unavailable": True})


def tool_timeout_metadata(source: str, **extra: Any) -> dict[str, Any]:
    return {
        **extra,
        "error": True,
        "timeout": True,
        "error_kind": "tool_timeout",
        "timeout_source": source,
    }


__all__ = ["ToolResult", "tool_timeout_metadata"]
