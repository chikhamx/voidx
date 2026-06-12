"""Tool definition filters shared by primary and worker-persona loops."""

from __future__ import annotations

from typing import Any


def filter_unavailable_lsp_tools(tool_defs: list[dict], lsp_manager: Any | None) -> list[dict]:
    if _has_available_lsp_server(lsp_manager):
        return tool_defs
    return [
        tool
        for tool in tool_defs
        if not str(tool.get("function", {}).get("name", "")).startswith("lsp_")
    ]


def _has_available_lsp_server(lsp_manager: Any | None) -> bool:
    if lsp_manager is None or not hasattr(lsp_manager, "has_available_server"):
        return False
    try:
        return bool(lsp_manager.has_available_server())
    except Exception:
        return False
