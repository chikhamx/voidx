"""MCP client package."""

from voidx.mcp.adapters.client.base import McpClient
from voidx.mcp.domain.errors import McpConnectionError, McpProtocolError, McpTimeoutError



def create_mcp_client(config):
    return McpClient(config)
__all__ = [
    "McpClient",
    "McpConnectionError",
    "McpProtocolError",
    "McpTimeoutError",
]
