"""MCP tool wrapper — adapts MCP server tools into voidx's BaseTool interface.

Each discovered MCP tool becomes a McpToolWrapper registered in ToolRegistry
with an LLM-safe id derived from the server and tool name.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from voidx.mcp.client import McpClient, McpConnectionError, McpProtocolError, McpTimeoutError
from voidx.mcp.schema import McpCallResult, McpToolDef
from voidx.tools.base import BaseTool, ToolResult, ToolContext


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

    def __init__(self, client: McpClient, tool_def: McpToolDef, server_name: str) -> None:
        self._client = client
        self._tool_def = tool_def
        self._server = server_name

    @property
    def id(self) -> str:
        return mcp_tool_id(self._server, self._tool_def.name)

    @property
    def description(self) -> str:
        desc = self._tool_def.description or f"MCP tool from {self._server}"
        return f"[MCP:{self._server}] {desc}"

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
        except (McpConnectionError, McpTimeoutError, McpProtocolError) as e:
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


def format_mcp_call_result(result: McpCallResult) -> str:
    parts = [_format_content_block(block) for block in result.content]
    if result.structured_content is not None:
        parts.append(
            "Structured content:\n"
            + json.dumps(result.structured_content, ensure_ascii=False, indent=2, default=str)
        )
    rendered = [part for part in parts if part]
    return "\n".join(rendered) if rendered else "(empty response)"


def _format_content_block(block: Any) -> str:
    if not isinstance(block, dict):
        return str(block) if block is not None else ""

    block_type = block.get("type", "")
    if block_type == "text":
        text = block.get("text", "")
        return text if isinstance(text, str) else str(text)

    if block_type == "image":
        mime = block.get("mimeType") or block.get("mime_type") or "unknown"
        data = block.get("data", "")
        size = len(data) if isinstance(data, str) else 0
        return f"[image {mime}, {size} base64 chars]"

    if block_type == "resource":
        resource = block.get("resource")
        if isinstance(resource, dict):
            uri = resource.get("uri", "resource")
            mime = resource.get("mimeType") or resource.get("mime_type") or ""
            header = f"[resource {uri}{f' ({mime})' if mime else ''}]"
            text = resource.get("text")
            if isinstance(text, str):
                return f"{header}\n{text}"
            blob = resource.get("blob")
            if isinstance(blob, str):
                return f"{header}\n[{len(blob)} base64 chars]"
            return header

    return json.dumps(block, ensure_ascii=False, sort_keys=True, default=str)
