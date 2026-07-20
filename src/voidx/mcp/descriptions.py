"""Configuration-only MCP server descriptions."""

from __future__ import annotations

from typing import Any


_MAX_TOOL_HINTS = 3
_MISSING_DESCRIPTION = "No description configured."


def configured_server_description(server: Any) -> str:
    """Return a safe MCP server description derived only from configuration."""
    configured = str(getattr(server, "description", "") or "").strip()
    if configured:
        return configured

    tool_names = configured_tool_names(getattr(server, "tools", None))
    if tool_names:
        visible = tool_names[:_MAX_TOOL_HINTS]
        suffix = ", ..." if len(tool_names) > _MAX_TOOL_HINTS else ""
        return f"Configured tools: {', '.join(visible)}{suffix}"

    return _MISSING_DESCRIPTION


def configured_tool_names(tools: Any) -> list[str]:
    if isinstance(tools, dict):
        return [
            str(name)
            for name, enabled in tools.items()
            if enabled is not False and str(name).strip()
        ]
    if isinstance(tools, list):
        return [str(name) for name in tools if str(name).strip()]
    return []
