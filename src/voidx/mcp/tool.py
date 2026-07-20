"""Legacy MCP tool wrapper — adapts MCP server tools into BaseTool instances.

Runtime MCP exposure now uses the stable gateway tool. This wrapper remains for
compatibility and focused behavior tests around MCP result formatting.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from voidx.mcp.client import McpClient, McpConnectionError, McpProtocolError, McpTimeoutError
from voidx.mcp.schema import McpCallResult, McpToolDef, format_mcp_call_result
from voidx.tools.base import BaseTool, ToolContext, ToolResult, tool_timeout_metadata


_LLM_TOOL_NAME_MAX = 64
_INVALID_TOOL_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def mcp_tool_id(server_name: str, tool_name: str) -> str:
    raw = f"mcp__{server_name}__{tool_name}"
    readable = _INVALID_TOOL_NAME_CHARS.sub("_", raw).strip("_") or "mcp_tool"
    digest = hashlib.sha1(f"{server_name}\0{tool_name}".encode("utf-8")).hexdigest()[:8]
    suffix = f"_{digest}"
    if len(readable) + len(suffix) > _LLM_TOOL_NAME_MAX:
        readable = readable[: _LLM_TOOL_NAME_MAX - len(suffix)].rstrip("_-") or "mcp_tool"
    return f"{readable}{suffix}"


class McpToolWrapper(BaseTool):
    """Wraps an MCP server tool as a voidx BaseTool."""

    id: str = "mcp_tool_placeholder"
    description: str = "MCP tool placeholder"

    def __init__(self, client: McpClient, tool_def: McpToolDef, server_name: str) -> None:
        self._client = client
        self._tool_def = tool_def
        self._server = server_name
        self.id = mcp_tool_id(server_name, tool_def.name)
        desc = tool_def.description or f"MCP tool from {server_name}"
        self.description = f"[MCP:{server_name}] {desc}"

    def parameters_schema(self) -> dict[str, Any]:
        return self._tool_def.inputSchema or {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not self._client.healthy:
            return ToolResult(
                output=f"MCP server '{self._server}' is not connected. "
                       f"Status: {self._client.status}."
                       f"{' ' + self._client.error_message if self._client.error_message else ''}",
                metadata={"error": True, "server": self._server},
            )
        try:
            result: McpCallResult = await self._client.call_tool(self._tool_def.name, args)
        except McpTimeoutError as e:
            return ToolResult(
                output=f"MCP server '{self._server}' timed out: {e}",
                metadata=tool_timeout_metadata(
                    "mcp",
                    server=self._server,
                    error_type=type(e).__name__,
                ),
            )
        except (McpConnectionError, McpProtocolError) as e:
            return ToolResult(
                output=f"MCP server '{self._server}' is unavailable: {e}",
                metadata={"error": True, "server": self._server, "error_type": type(e).__name__},
            )
        except Exception as e:
            return ToolResult(
                output=f"MCP tool '{self._tool_def.name}' error: {e}",
                metadata={"error": True, "server": self._server, "error_type": type(e).__name__},
            )

        output = format_mcp_call_result(result)
        meta: dict[str, Any] = {"server": self._server}
        if result.isError:
            meta["error"] = True

        return ToolResult(output=output, metadata=meta)
