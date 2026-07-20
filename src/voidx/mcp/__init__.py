"""MCP (Model Context Protocol) support for voidx.

Provides:
  - McpManager: lifecycle orchestrator for all MCP server connections
  - McpClient: low-level JSON-RPC 2.0 client over stdio transport
  - McpToolWrapper: legacy adapter for MCP tools as BaseTool instances
  - McpRuntimeStatus: server status for UI display

Usage (automatic, via agent graph):
  1. Configure servers in .voidx/settings.json under "mcpServers"
  2. McpManager starts them on graph.run()
  3. Discovered tools are cataloged behind the stable "mcp" gateway tool
  4. Permission rules support "mcp@pattern:mcp:{server}:{tool}" resources
"""

from __future__ import annotations

from voidx.mcp.manager import McpManager
from voidx.mcp.client import McpClient, McpConnectionError, McpProtocolError, McpTimeoutError
from voidx.mcp.tool import McpToolWrapper
from voidx.mcp.schema import McpRuntimeStatus, McpToolDef, McpCallResult, format_mcp_call_result

__all__ = [
    "McpManager",
    "McpClient",
    "McpToolWrapper",
    "McpRuntimeStatus",
    "McpToolDef",
    "McpCallResult",
    "format_mcp_call_result",
    "McpConnectionError",
    "McpProtocolError",
    "McpTimeoutError",
]
