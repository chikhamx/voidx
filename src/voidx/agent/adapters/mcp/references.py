"""Resolve $name MCP server references in user messages.

Like skill references, a selected `$name` injects a semantic server summary
and removes the token from the visible user message.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from voidx.mcp.context import render_mcp_server_summary
from voidx.mcp.descriptions import configured_server_description
from voidx.agent.domain.turn.references import EXPLICIT_REF_RE


@dataclass(frozen=True)
class McpReferenceMessage:
    prefix: str = ""
    remove_spans: list[tuple[int, int]] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)


async def mcp_reference_message(
    user_text: str,
    *,
    settings=None,
    manager=None,
) -> McpReferenceMessage:
    if "$" not in user_text or manager is None or settings is None:
        return McpReferenceMessage()

    configured = {
        server.name: server
        for server in settings.list_mcp_servers()
        if not server.disabled
    }
    if not configured:
        return McpReferenceMessage()

    statuses = {s.name: s for s in manager.statuses()}
    catalog = {e.name: e for e in getattr(manager, "catalog_snapshot", lambda: [])()}
    seen: set[str] = set()
    remove_spans: list[tuple[int, int]] = []
    prefixes: list[str] = []
    servers: list[str] = []

    for match in EXPLICIT_REF_RE.finditer(user_text):
        name = match.group(1)
        if name not in configured or name in seen:
            continue
        seen.add(name)
        remove_spans.append((match.start(), match.end()))
        status = statuses.get(name)
        config = configured[name]
        state = status.status if status is not None else "unknown"
        entry = catalog.get(name)
        prefixes.append(
            render_mcp_server_summary(
                name,
                status=state,
                description=_server_description(manager, name, config),
                server_info=entry.server_info if entry is not None else None,
            )
        )
        servers.append(name)

    if not prefixes:
        return McpReferenceMessage(remove_spans=remove_spans)
    return McpReferenceMessage(
        prefix="\n\n".join(prefixes),
        remove_spans=remove_spans,
        servers=servers,
    )


def _server_description(manager, name: str, config) -> str:
    getter = getattr(manager, "server_description", None)
    if callable(getter):
        description = str(getter(name) or "").strip()
        if description:
            return description
    return configured_server_description(config)
