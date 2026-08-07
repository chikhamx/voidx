"""MCP client port and factory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from voidx.mcp.domain.config import McpServerConfig
from voidx.mcp.schema import McpCallResult, McpToolDef


class McpClient(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def reconnect(self) -> bool: ...
    async def list_tools(self) -> list[McpToolDef]: ...
    async def call_tool(self, name: str, arguments: dict) -> McpCallResult: ...


McpClientFactory = Callable[[McpServerConfig], McpClient]

__all__ = ["McpClient", "McpClientFactory"]
