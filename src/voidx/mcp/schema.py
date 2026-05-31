"""MCP protocol types — JSON-RPC wire format, tool definitions, call results.

Based on the Model Context Protocol specification:
  https://spec.modelcontextprotocol.io/
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
        "version": "1.0.0",
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
    structured_content: Any = None


# ── Server status (for UI consumption) ────────────────────────────────────


@dataclass
class McpRuntimeStatus:
    """Runtime status of a single MCP server connection."""
    name: str
    status: str  # "connected" | "disconnected" | "error"
    tool_count: int = 0
    error_message: str = ""
