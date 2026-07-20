"""Render configuration-only MCP server discovery for the stable system prefix."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voidx.config import Settings
    from voidx.config.models import McpServerConfig

from voidx.mcp.descriptions import configured_server_description


def render_available_mcp_servers(
    settings: Settings | None,
    *,
    descriptions: Mapping[str, str] | None = None,
) -> str:
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
    lines.extend(_server_summary(server, descriptions) for server in servers)
    return "\n".join(lines)


def _server_summary(
    server: McpServerConfig,
    descriptions: Mapping[str, str] | None = None,
) -> str:
    generated = (descriptions or {}).get(server.name, "").strip()
    return f"- {server.name}: {generated or configured_server_description(server)}"
