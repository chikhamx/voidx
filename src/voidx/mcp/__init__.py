"""MCP (Model Context Protocol) support for voidx.

Provides:
  - McpManager: lifecycle orchestrator for all MCP server connections
  - McpClient: low-level JSON-RPC 2.0 client over stdio transport
  - McpToolWrapper: adapts MCP tools into voidx's BaseTool interface
  - McpRuntimeStatus: server status for UI display

Usage (automatic, via agent graph):
  1. Configure servers in voidx.json under "mcpServers"
  2. McpManager starts them on graph.run()
  3. Tools appear with id "mcp__{server}__{tool}_{hash}" in LLM function list
  4. Permission rules support "mcp__*" wildcard for blanket approval
"""

from __future__ import annotations

from voidx.mcp.manager import McpManager
from voidx.mcp.client import McpClient, McpConnectionError, McpProtocolError, McpTimeoutError
from voidx.mcp.tool import McpToolWrapper
from voidx.mcp.schema import McpRuntimeStatus, McpToolDef, McpCallResult

__all__ = [
    "McpManager",
    "McpClient",
    "McpToolWrapper",
    "McpRuntimeStatus",
    "McpToolDef",
    "McpCallResult",
    "McpConnectionError",
    "McpProtocolError",
    "McpTimeoutError",
]
