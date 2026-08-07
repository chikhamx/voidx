"""Narrow MCP ports used by Tooling integration adapters."""

from __future__ import annotations

from typing import Protocol


class McpToolCaller(Protocol):
    async def call_tool(self, server: str, tool: str, arguments: dict) -> object: ...


class McpGateway(McpToolCaller, Protocol):
    def statuses(self) -> list[object]: ...
    def catalog_snapshot(self) -> list[object]: ...
    def server_config(self, name: str) -> object | None: ...
    def tool_def(self, server: str, tool: str) -> object | None: ...


__all__ = ["McpGateway", "McpToolCaller"]
