"""MCP client exception types."""

from __future__ import annotations


class McpConnectionError(Exception):
    """Connection-level error (process died, transport failure)."""


class McpProtocolError(Exception):
    """Protocol-level error (invalid JSON, unexpected response)."""


class McpTimeoutError(Exception):
    """Operation timed out."""
