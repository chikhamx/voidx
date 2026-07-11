import json
import logging
import sys
from pathlib import Path

import pytest


from voidx.config import Settings, WebToolRoute
from voidx.mcp.schema import McpCallResult
from voidx.tools.base import ToolContext
from voidx.tools.web.content import WEB_TOOL_CACHE
from voidx.tools.web.fetch import WebFetchTool, _FetchResponse
from voidx.tools.web.search import WebSearchTool
from voidx.tools.web import fetch as webfetch_module
from voidx.tools.web import search as websearch_module


class FakeMcpManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        return McpCallResult(content=[{"type": "text", "text": "mcp ok"}])


def test_duckduckgo_parser_returns_empty_on_parse_failure(monkeypatch):
    def broken_feed(self, html):
        raise RuntimeError("parser broke")

    monkeypatch.setattr(websearch_module._DDGResultParser, "feed", broken_feed)

    results = websearch_module._parse_duckduckgo_html("<html>")

    assert results == []


@pytest.mark.asyncio
async def test_websearch_delegates_to_configured_mcp_route(tmp_path):
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "web": {
                "search": {
                    "backend": "mcp",
                    "server": "voidx-web",
                    "tool": "web_search",
                }
            }
        }),
        encoding="utf-8",
    )
    manager = FakeMcpManager()
    settings = Settings(str(tmp_path))
    tool = WebSearchTool(settings=settings)

    result = await tool.execute(
        {
            "query": "voidx",
            "allowed_domains": ["example.com"],
            "blocked_domains": ["old.example.com"],
            "max_results": 5,
        },
        ToolContext(workspace=str(tmp_path), mcp_manager=manager),
    )

    assert result.output == "mcp ok"
    assert result.metadata["backend"] == "mcp"
    assert manager.calls == [
        (
            "voidx-web",
            "web_search",
            {
                "query": "voidx",
                "allowed_domains": ["example.com"],
                "blocked_domains": ["old.example.com"],
                "max_results": 5,
            },
        )
    ]


@pytest.mark.asyncio
async def test_websearch_adapts_arguments_for_tavily_mcp(tmp_path):
    settings = Settings(str(tmp_path))
    settings.set_web_tool_route(
        "search",
        WebToolRoute(backend="mcp", server="tavily", tool="tavily_search"),
    )
    manager = FakeMcpManager()
    tool = WebSearchTool(settings=settings)

    result = await tool.execute(
        {
            "query": "voidx",
            "allowed_domains": ["docs.example.com"],
            "blocked_domains": ["old.example.com"],
            "max_results": 3,
        },
        ToolContext(workspace=str(tmp_path), mcp_manager=manager),
    )

    assert result.output == "mcp ok"
    assert manager.calls == [
        (
            "tavily",
            "tavily_search",
            {
                "query": "voidx",
                "include_domains": ["docs.example.com"],
                "exclude_domains": ["old.example.com"],
                "max_results": 3,
            },
        )
    ]


@pytest.mark.asyncio
async def test_webfetch_delegates_to_configured_mcp_route(tmp_path):
    settings = Settings(str(tmp_path))
    settings.set_web_tool_route(
        "fetch",
        WebToolRoute(backend="mcp", server="voidx-web", tool="web_fetch"),
    )
    manager = FakeMcpManager()
    tool = WebFetchTool(settings=settings)

    result = await tool.execute(
        {"url": "https://example.com", "prompt": "extract title"},
        ToolContext(workspace=str(tmp_path), mcp_manager=manager),
    )

    assert result.output == "mcp ok"
    assert manager.calls == [
        ("voidx-web", "web_fetch", {"url": "https://example.com", "prompt": "extract title"})
    ]


@pytest.mark.asyncio
async def test_webfetch_adapts_arguments_for_tavily_mcp(tmp_path):
    settings = Settings(str(tmp_path))
    settings.set_web_tool_route(
        "fetch",
        WebToolRoute(backend="mcp", server="tavily", tool="tavily_extract"),
    )
    manager = FakeMcpManager()
    tool = WebFetchTool(settings=settings)

    result = await tool.execute(
        {"url": "https://example.com", "prompt": "extract title"},
        ToolContext(workspace=str(tmp_path), mcp_manager=manager),
    )

    assert result.output == "mcp ok"
    assert manager.calls == [
        ("tavily", "tavily_extract", {"urls": ["https://example.com"], "query": "extract title"})
    ]


@pytest.mark.asyncio
async def test_websearch_returns_structured_normalized_results(tmp_path, monkeypatch):
    WEB_TOOL_CACHE.clear()
    tool = WebSearchTool(settings=Settings(str(tmp_path)))
    monkeypatch.setattr(WebSearchTool, "_get_tavily_key", lambda self: None)

    async def fake_search(*args, **kwargs):
        return [
            {
                "title": " Example Result ",
                "url": "https://example.com/docs?utm_source=x#top",
                "snippet": " Useful docs ",
            },
            {
                "title": "Duplicate",
                "url": "https://example.com/docs",
                "snippet": "duplicate",
            },
        ]

    monkeypatch.setattr(websearch_module, "_search_duckduckgo", fake_search)

    result = await tool.execute({"query": "voidx docs", "max_results": 5}, ToolContext(workspace=str(tmp_path)))

    assert result.metadata["backend"] == "duckduckgo"
    assert result.metadata["cached"] is False
    assert result.metadata["results"] == 1
    assert result.metadata["items"][0]["url"] == "https://example.com/docs"
    assert result.metadata["items"][0]["domain"] == "example.com"
    assert "[Example Result](https://example.com/docs)" in result.output


@pytest.mark.asyncio
async def test_websearch_uses_ttl_cache(tmp_path, monkeypatch):
    WEB_TOOL_CACHE.clear()
    tool = WebSearchTool(settings=Settings(str(tmp_path)))
    calls = 0
    monkeypatch.setattr(WebSearchTool, "_get_tavily_key", lambda self: None)

    async def fake_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        return [{"title": "Cached", "url": "https://example.com", "snippet": "ok"}]

    monkeypatch.setattr(websearch_module, "_search_duckduckgo", fake_search)

    first = await tool.execute({"query": "cache me"}, ToolContext(workspace=str(tmp_path)))
    second = await tool.execute({"query": "cache me"}, ToolContext(workspace=str(tmp_path)))

    assert calls == 1
    assert first.metadata["cached"] is False
    assert second.metadata["cached"] is True


@pytest.mark.asyncio
async def test_webfetch_extracts_readable_html_and_metadata(tmp_path, monkeypatch):
    WEB_TOOL_CACHE.clear()
    html = """
    <html>
      <head><title>Install Guide</title><script>bad()</script></head>
      <body>
        <nav>menu</nav>
        <main>
          <h1>Install voidx</h1>
          <p>Install voidx with pip.</p>
          <pre>voidx --help</pre>
        </main>
      </body>
    </html>
    """

    async def fake_fetch(url: str):
        return _FetchResponse(
            url="https://example.com/docs?utm_source=x#install",
            status_code=200,
            text=html,
            content_type="text/html; charset=utf-8",
        )

    monkeypatch.setattr(webfetch_module, "_fetch_url", fake_fetch)
    tool = WebFetchTool(settings=Settings(str(tmp_path)))

    result = await tool.execute(
        {"url": "https://example.com/docs", "prompt": "install"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata["title"] == "Install Guide"
    assert result.metadata["canonical_url"] == "https://example.com/docs"
    assert result.metadata["cached"] is False
    assert "## Relevant excerpts" in result.output
    assert "# Install voidx" in result.output
    assert "Install voidx with pip." in result.output
    assert "menu" not in result.output
    assert "bad()" not in result.output


@pytest.mark.asyncio
async def test_webfetch_uses_ttl_cache(tmp_path, monkeypatch):
    WEB_TOOL_CACHE.clear()
    calls = 0

    async def fake_fetch(url: str):
        nonlocal calls
        calls += 1
        return _FetchResponse(
            url="https://example.com",
            status_code=200,
            text="<html><body><p>cached page</p></body></html>",
            content_type="text/html",
        )

    monkeypatch.setattr(webfetch_module, "_fetch_url", fake_fetch)
    tool = WebFetchTool(settings=Settings(str(tmp_path)))

    first = await tool.execute({"url": "https://example.com", "prompt": "page"}, ToolContext(workspace=str(tmp_path)))
    second = await tool.execute({"url": "https://example.com", "prompt": "page"}, ToolContext(workspace=str(tmp_path)))

    assert calls == 1
    assert first.metadata["cached"] is False
    assert second.metadata["cached"] is True


@pytest.mark.asyncio
async def test_mcp_web_timeout_uses_unified_metadata(tmp_path):
    from voidx.mcp.client import McpTimeoutError

    class TimeoutManager:
        async def call_tool(self, server, tool, arguments):
            raise McpTimeoutError("request timed out")

    settings = Settings(str(tmp_path))
    settings.set_web_tool_route(
        "search",
        WebToolRoute(backend="mcp", server="test-server", tool="search"),
    )

    result = await WebSearchTool(settings=settings).execute(
        {"query": "timeout"},
        ToolContext(workspace=str(tmp_path), mcp_manager=TimeoutManager()),
    )

    assert result.metadata["error"] is True
    assert result.metadata["timeout"] is True
    assert result.metadata["error_kind"] == "tool_timeout"
    assert result.metadata["timeout_source"] == "mcp"
    assert result.metadata["backend"] == "mcp"


@pytest.mark.asyncio
async def test_direct_websearch_timeout_uses_unified_metadata(tmp_path, monkeypatch):
    import httpx

    WEB_TOOL_CACHE.clear()

    async def timeout_search(*args, **kwargs):
        raise httpx.ReadTimeout("search timed out")

    monkeypatch.setattr(WebSearchTool, "_get_tavily_key", lambda self: None)
    monkeypatch.setattr(websearch_module, "_search_duckduckgo", timeout_search)

    result = await WebSearchTool(settings=Settings(str(tmp_path))).execute(
        {"query": "timeout"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata["error"] is True
    assert result.metadata["timeout"] is True
    assert result.metadata["error_kind"] == "tool_timeout"
    assert result.metadata["timeout_source"] == "web"


@pytest.mark.asyncio
async def test_websearch_fallback_succeeds_when_first_backend_times_out(tmp_path, monkeypatch):
    """When the first search backend times out but the fallback succeeds,
    the result must be successful, not a timeout."""
    import httpx

    WEB_TOOL_CACHE.clear()

    async def tavily_timeout(*args, **kwargs):
        raise httpx.ReadTimeout("tavily timed out")

    async def duckduckgo_success(*args, **kwargs):
        return [
            {
                "title": "Fallback Result",
                "url": "https://fallback.example.com",
                "snippet": "from duckduckgo",
            },
        ]

    monkeypatch.setattr(WebSearchTool, "_get_tavily_key", lambda self: "fake-key")
    monkeypatch.setattr(websearch_module, "_search_tavily", tavily_timeout)
    monkeypatch.setattr(websearch_module, "_search_duckduckgo", duckduckgo_success)

    result = await WebSearchTool(settings=Settings(str(tmp_path))).execute(
        {"query": "fallback test"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("error") is not True
    assert result.metadata.get("timeout") is not True
    assert result.metadata.get("backend") == "duckduckgo"
    assert result.metadata.get("results") == 1
    assert "Fallback Result" in result.output
