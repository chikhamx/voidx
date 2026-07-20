"""MCP catalog — in-memory source of truth for discovered MCP tools.

McpManager writes filtered tool definitions here after server discovery.
Direct tool wrappers, the mcp gateway tool, and UI status all read from the
same snapshot so they never disagree about what is available.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from voidx.mcp.schema import McpToolDef


@dataclass(frozen=True)
class McpServerCatalogEntry:
    """Filtered tool definitions discovered from one MCP server."""

    name: str
    tools: tuple[McpToolDef, ...]
    server_info: dict = field(default_factory=dict)
    instructions: str = ""


class McpCatalog:
    """Per-session in-memory catalog of filtered MCP tool definitions."""

    def __init__(self) -> None:
        self._entries: dict[str, McpServerCatalogEntry] = {}

    def put(
        self,
        server: str,
        tool_defs: list[McpToolDef],
        *,
        server_info: dict | None = None,
        instructions: str = "",
    ) -> None:
        self._entries[server] = McpServerCatalogEntry(
            name=server,
            tools=tuple(tool_defs),
            server_info=server_info or {},
            instructions=instructions,
        )

    def remove(self, server: str) -> None:
        self._entries.pop(server, None)

    def clear(self) -> None:
        self._entries.clear()

    def snapshot(self) -> list[McpServerCatalogEntry]:
        return list(self._entries.values())

    def tool_def(self, server: str, tool: str) -> McpToolDef | None:
        entry = self._entries.get(server)
        if entry is None:
            return None
        for tool_def in entry.tools:
            if tool_def.name == tool:
                return tool_def
        return None

    def tool_count(self, server: str) -> int:
        entry = self._entries.get(server)
        return len(entry.tools) if entry is not None else 0
