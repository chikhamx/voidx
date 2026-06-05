"""MCP protocol types — JSON-RPC wire format, tool definitions, call results.

Based on the Model Context Protocol specification:
  https://spec.modelcontextprotocol.io/
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.metadata import version as _pkg_version
from typing import Any


# ── JSON-RPC 2.0 ──────────────────────────────────────────────────────────


@dataclass
class JsonRpcRequest:
    """A JSON-RPC 2.0 request message."""
    jsonrpc: str = "2.0"
    id: int = 0
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self.id,
            "method": self.method,
        }
        if self.params:
            d["params"] = self.params
        return d


@dataclass
class JsonRpcResponse:
    """A JSON-RPC 2.0 response message."""
    id: int = 0
    result: Any = None
    error: dict[str, Any] | None = None


@dataclass
class JsonRpcNotification:
    """A JSON-RPC 2.0 notification (no id, no response expected)."""
    jsonrpc: str = "2.0"
    method: str = ""
    params: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": self.method,
        }
        if self.params:
            d["params"] = self.params
        return d


# ── MCP protocol messages ─────────────────────────────────────────────────


MCP_PROTOCOL_VERSION = "2025-03-26"


@dataclass
class McpInitializeParams:
    """Parameters for the initialize request."""
    protocol_version: str = MCP_PROTOCOL_VERSION
    capabilities: dict[str, Any] = field(default_factory=lambda: {
        "roots": {"listChanged": False},
        "sampling": {},
    })
    client_info: dict[str, str] = field(default_factory=lambda: {
        "name": "voidx",
        "version": _pkg_version("voidx"),
    })

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": self.capabilities,
            "clientInfo": self.client_info,
        }


@dataclass
class McpToolDef:
    """A tool exposed by an MCP server."""
    name: str
    description: str = ""
    inputSchema: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpCallResult:
    """Result from a tools/call request."""
    content: list[dict[str, Any]] = field(default_factory=list)
    isError: bool = False
    structured_content: dict[str, Any] | None = None


# ── Server status (for UI consumption) ────────────────────────────────────


@dataclass
class McpRuntimeStatus:
    """Runtime status of a single MCP server connection."""
    name: str
    status: str  # "connected" | "connecting" | "disconnected" | "error" | "disabled"
    tool_count: int = 0
    error_message: str = ""


# ── Result formatting ──────────────────────────────────────────────────────


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
