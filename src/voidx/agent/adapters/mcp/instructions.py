"""MCP instruction renderer adapter."""

from collections.abc import Mapping
from typing import Any

from voidx.mcp.auto import render_available_mcp_servers


def render_available_servers(settings: Any | None, descriptions: Mapping[str, str] | None = None) -> str:
    return render_available_mcp_servers(settings, descriptions=descriptions)


__all__ = ["render_available_servers"]
