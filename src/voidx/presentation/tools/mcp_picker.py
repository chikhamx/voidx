"""MCP server picker helpers for # references."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from voidx.config import Settings
from voidx.mcp.descriptions import configured_server_description


@dataclass(frozen=True)
class McpCandidate:
    name: str
    description: str
    mode: str


def list_mcp_candidates(
    workspace: str,
    query: str,
    limit: int = 8,
    *,
    settings=None,
    catalog: Iterable | None = None,
) -> list[McpCandidate]:
    if settings is None:
        settings = Settings(workspace)
    query_lower = query.strip().lower()
    catalog_by_name = {
        str(entry.name): entry
        for entry in (catalog or [])
        if getattr(entry, "name", None)
    }
    candidates: list[McpCandidate] = []
    for server in settings.list_mcp_servers():
        if server.disabled:
            continue
        mode = "auto" if server.auto else "manual"
        description = _resolve_description(server, catalog_by_name.get(server.name))
        candidate = McpCandidate(
            name=server.name,
            description=description,
            mode=mode,
        )
        name_lower = server.name.lower()
        desc_lower = description.lower()
        if not query_lower:
            candidates.append(candidate)
        elif name_lower.startswith(query_lower):
            candidates.append(candidate)
        elif query_lower in name_lower or query_lower in desc_lower:
            candidates.append(candidate)
    candidates.sort(key=lambda c: c.name.lower())
    return candidates[:limit]


def _resolve_description(server, catalog_entry=None) -> str:
    catalog_description = str(getattr(catalog_entry, "description", "") or "").strip()
    if catalog_description:
        return catalog_description
    return configured_server_description(server)
