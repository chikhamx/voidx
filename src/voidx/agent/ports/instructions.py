"""Ports and neutral defaults for optional instruction sections."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

AvailableServersRenderer = Callable[[Any | None, Mapping[str, str] | None], str]


def render_available_servers(settings: Any | None, descriptions: Mapping[str, str] | None = None) -> str:
    if settings is None:
        return ""
    servers = sorted(
        (server for server in settings.list_mcp_servers() if server.auto and not server.disabled),
        key=lambda server: server.name.lower(),
    )
    if not servers:
        return ""
    lines = [
        "## Available MCP Servers",
        (
            "These server summaries are capability hints, not loaded tool documentation. "
            "When a server is relevant to the task, use `mcp(op=\"load\", server=\"<name>\")` "
            "to inspect its tools and parameters before calling it."
        ),
    ]
    for server in servers:
        generated = (descriptions or {}).get(server.name, "").strip()
        lines.append(f"- {server.name}: {generated or _configured_description(server)}")
    return "\n".join(lines)


def _configured_description(server: Any) -> str:
    configured = str(getattr(server, "description", "") or "").strip()
    if configured:
        return configured
    tools = getattr(server, "tools", None)
    if isinstance(tools, dict):
        names = [str(name) for name, enabled in tools.items() if enabled is not False and str(name).strip()]
    elif isinstance(tools, list):
        names = [str(name) for name in tools if str(name).strip()]
    else:
        names = []
    if names:
        visible = names[:3]
        suffix = ", ..." if len(names) > 3 else ""
        return f"Configured tools: {', '.join(visible)}{suffix}"
    return "No description configured."
