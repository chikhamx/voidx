"""Web tools — search and fetch. Tavily MCP > built-in fallback."""

from __future__ import annotations

from .search import WebSearchInput, WebSearchTool
from .fetch import WebFetchInput, WebFetchTool

__all__ = [
    "WebSearchTool",
    "WebSearchInput",
    "WebFetchTool",
    "WebFetchInput",
]