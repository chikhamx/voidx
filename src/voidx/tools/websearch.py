"""WebSearch tool — search the web. Tavily > DuckDuckGo fallback."""

from __future__ import annotations

import logging
import os
from html.parser import HTMLParser

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult
from voidx.tools.web_content import (
    WEB_TOOL_CACHE,
    cached_tool_result,
    matches_domain,
    normalize_search_results,
    search_cache_key,
)
from voidx.tools.web_mcp import call_mcp_web_tool

_logger = logging.getLogger(__name__)


class WebSearchInput(BaseModel):
    query: str = Field(description="Search query, max 70 characters")
    allowed_domains: list[str] | None = Field(
        default=None,
        description="Only include search results from these domains",
    )
    blocked_domains: list[str] | None = Field(
        default=None,
        description="Never include search results from these domains",
    )
    max_results: int = Field(default=10, ge=1, le=20, description="Maximum number of results to return")


# ── DuckDuckGo HTML parser ──────────────────────────────────────────────

class _DDGResultParser(HTMLParser):
    """Extract search results from DuckDuckGo HTML page."""

    def __init__(self):
        super().__init__()
        self._results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture = ""
        self._in_result_link = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr = dict(attrs)
        cls = attr.get("class", "")

        if tag == "a" and "result__a" in cls:
            self._current = {"url": attr.get("href", ""), "title": "", "snippet": ""}
            self._in_result_link = True
            self._capture = ""

        if tag == "a" and "result__snippet" in cls and self._current is not None:
            self._in_snippet = True
            self._capture = ""

    def handle_endtag(self, tag: str):
        if tag == "a" and self._in_result_link:
            if self._current is not None:
                self._current["title"] = self._capture.strip()
            self._in_result_link = False

        if tag == "a" and self._in_snippet:
            if self._current is not None:
                self._current["snippet"] = self._capture.strip()
                self._results.append(self._current)
                self._current = None
            self._in_snippet = False

    def handle_data(self, data: str):
        if self._in_result_link or self._in_snippet:
            self._capture += data

    def results(self) -> list[dict[str, str]]:
        return self._results


def _parse_duckduckgo_html(html: str) -> list[dict[str, str]]:
    """Parse DuckDuckGo HTML page using HTMLParser (not fragile regex)."""
    parser = _DDGResultParser()
    try:
        parser.feed(html)
    except Exception:
        _logger.debug("DuckDuckGo HTML parse failed", exc_info=True)
    return parser.results()


# ── Tavily API backend ──────────────────────────────────────────────────

async def _search_tavily(
    query: str,
    api_key: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    max_results: int = 10,
) -> list[dict[str, str]]:
    """Search via Tavily API. Returns list of {url, title, snippet}."""
    import httpx

    url = "https://api.tavily.com/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "query": query,
        "max_results": max_results,
        "include_answer": False,
        "search_depth": "basic",
    }
    if allowed_domains:
        payload["include_domains"] = allowed_domains
    if blocked_domains:
        payload["exclude_domains"] = blocked_domains

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    return [
        {
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "snippet": item.get("content", ""),
        }
        for item in data.get("results", [])
    ]


# ── DuckDuckGo fallback backend ─────────────────────────────────────────

async def _search_duckduckgo(
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    max_results: int = 10,
) -> list[dict[str, str]]:
    """Search via DuckDuckGo HTML scraping. Returns list of {url, title, snippet}."""
    import httpx

    search_url = "https://html.duckduckgo.com/html/"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            search_url,
            data={"q": query},
            headers={"User-Agent": "voidx/0.1"},
        )
        resp.raise_for_status()

    results = _parse_duckduckgo_html(resp.text)

    if allowed_domains:
        results = [r for r in results if any(matches_domain(r["url"], domain) for domain in allowed_domains)]
    if blocked_domains:
        results = [r for r in results if not any(matches_domain(r["url"], domain) for domain in blocked_domains)]

    return results[:max_results]


# ── Tool ────────────────────────────────────────────────────────────────

class WebSearchTool(BaseTool):
    id = "websearch"
    description = "Search the web. Returns titles, URLs, and snippets."

    def __init__(self, settings=None):
        super().__init__()
        self._settings = settings

    def parameters_schema(self) -> dict:
        return model_to_json_schema(WebSearchInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = WebSearchInput.model_validate(args)
        mcp_result = await call_mcp_web_tool(
            kind="search",
            settings=self._settings,
            ctx=ctx,
            arguments=inp.model_dump(exclude_none=True),
            title=f"Search: {inp.query}",
        )
        if mcp_result is not None:
            return mcp_result

        tavily_key = self._get_tavily_key()
        cache_key = search_cache_key(
            query=inp.query,
            allowed_domains=inp.allowed_domains,
            blocked_domains=inp.blocked_domains,
            max_results=inp.max_results,
            backend="tavily" if tavily_key else "duckduckgo",
        )
        cached = WEB_TOOL_CACHE.get(cache_key)
        if isinstance(cached, ToolResult):
            return cached_tool_result(cached)

        fallback_errors: list[str] = []
        if tavily_key:
            try:
                results = await _search_tavily(
                    inp.query,
                    tavily_key,
                    inp.allowed_domains,
                    inp.blocked_domains,
                    inp.max_results,
                )
                if results:
                    result = self._format_results(inp.query, results, "tavily", fallback_errors)
                    WEB_TOOL_CACHE.set(cache_key, result, ttl_seconds=600)
                    return result
            except Exception as exc:
                fallback_errors.append(f"tavily: {exc}")

        try:
            results = await _search_duckduckgo(
                inp.query,
                inp.allowed_domains,
                inp.blocked_domains,
                inp.max_results,
            )
        except Exception as e:
            return ToolResult(
                output=f"Search failed: {e}. Query: {inp.query}",
                metadata={"query": inp.query, "error": str(e), "fallback_errors": fallback_errors},
            )

        if not results:
            return ToolResult(
                output=f"No results found for: {inp.query}",
                metadata={
                    "query": inp.query,
                    "results": 0,
                    "backend": "duckduckgo",
                    "fallback_errors": fallback_errors,
                },
            )

        result = self._format_results(inp.query, results, "duckduckgo", fallback_errors)
        WEB_TOOL_CACHE.set(cache_key, result, ttl_seconds=600)
        return result

    def _get_tavily_key(self) -> str | None:
        env_key = os.environ.get("TAVILY_API_KEY")
        if env_key:
            return env_key
        if self._settings:
            return self._settings.get_tavily_api_key()
        return None

    @staticmethod
    def _format_results(
        query: str,
        results: list[dict[str, str]],
        backend: str,
        fallback_errors: list[str] | None = None,
    ) -> ToolResult:
        normalized = normalize_search_results(results)
        formatted = []
        for r in normalized:
            snippet = f"\n  {r['snippet']}" if r["snippet"] else ""
            formatted.append(f"- [{r['title']}]({r['url']}){snippet}")
        return ToolResult(
            title=f"Search: {query}",
            output="\n\n".join(formatted),
            summary=f"{len(normalized)} results",
            metadata={
                "query": query,
                "results": len(normalized),
                "backend": backend,
                "items": normalized,
                "cached": False,
                "fallback_errors": fallback_errors or [],
            },
        )
