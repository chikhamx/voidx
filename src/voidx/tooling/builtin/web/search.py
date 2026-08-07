"""WebSearch tool — search the web. Tavily > DuckDuckGo fallback."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable
from html.parser import HTMLParser

import httpx
from pydantic import BaseModel, Field

from voidx.logging.tool_log import log_tool_event
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import (
    ToolResult,
    tool_timeout_metadata,
)
from voidx.tooling.domain.schema import model_to_json_schema
from .content import (
    WEB_TOOL_CACHE,
    cached_tool_result,
    matches_domain,
    normalize_search_results,
    search_cache_key,
)
from voidx.tooling.ports.web_route import WebRoute

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
    except Exception as exc:
        log_tool_event("websearch_parse_failed", tool_name="websearch", message=f"DuckDuckGo HTML parse failed: {exc}")
    return parser.results()
# ── Bocha API backend ────────────────────────────────────────────────────

async def _search_bocha(
    query: str,
    api_key: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    max_results: int = 10,
) -> list[dict[str, str]]:
    """Search via Bocha API. Returns list of {url, title, snippet}."""
    payload = {"query": query, "freshness": "oneYear", "summary": True, "count": max_results}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.bochaai.com/v1/web-search",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    results = [
        {
            "url": item.get("url", ""),
            "title": item.get("name", ""),
            "snippet": item.get("summary") or item.get("snippet", ""),
        }
        for item in data.get("webPages", {}).get("value", [])
    ]
    return _filter_results(results, allowed_domains, blocked_domains)[:max_results]


# ── Bing HTML backend ────────────────────────────────────────────────────

class _BingResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture = ""
        self._in_title = False
        self._in_snippet = False
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr = dict(attrs)
        if tag == "li" and "b_algo" in (attr.get("class") or "").split():
            self._current = {"url": "", "title": "", "snippet": ""}
            self._depth = 1
            return
        if self._current is None:
            return
        self._depth += 1
        if tag == "a" and not self._current["url"]:
            href = attr.get("href") or ""
            if href.startswith(("http://", "https://")):
                self._current["url"] = href
                self._in_title = True
                self._capture = ""
        elif tag == "p":
            self._in_snippet = True
            self._capture = ""

    def handle_endtag(self, tag: str):
        if self._current is None:
            return
        if tag == "a" and self._in_title:
            self._current["title"] = self._capture.strip()
            self._in_title = False
        elif tag == "p" and self._in_snippet:
            self._current["snippet"] = self._capture.strip()
            self._in_snippet = False
        self._depth -= 1
        if tag == "li" and self._depth <= 0:
            if self._current["url"]:
                self._results.append(self._current)
            self._current = None
            self._depth = 0

    def handle_data(self, data: str):
        if self._in_title or self._in_snippet:
            self._capture += data

    def results(self) -> list[dict[str, str]]:
        return self._results


def _parse_bing_html(html: str) -> list[dict[str, str]]:
    parser = _BingResultParser()
    try:
        parser.feed(html)
    except Exception as exc:
        log_tool_event("websearch_parse_failed", tool_name="websearch", message=f"Bing HTML parse failed: {exc}")
    return parser.results()


def _filter_results(results, allowed_domains, blocked_domains):
    if allowed_domains:
        results = [r for r in results if any(matches_domain(r["url"], d) for d in allowed_domains)]
    if blocked_domains:
        results = [r for r in results if not any(matches_domain(r["url"], d) for d in blocked_domains)]
    return results


async def _search_bing(
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    max_results: int = 10,
) -> list[dict[str, str]]:
    from urllib.parse import quote
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
        "Accept-Language": "en-US,en;q=0.5",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"https://cn.bing.com/search?q={quote(query)}", headers=headers)
        resp.raise_for_status()
    return _filter_results(_parse_bing_html(resp.text), allowed_domains, blocked_domains)[:max_results]




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

class WebSearchTool:
    id = "websearch"
    description = "Search the web. Returns titles, URLs, and snippets."

    def __init__(self, settings=None, web_route: WebRoute | None = None):
        super().__init__()
        self._settings = settings
        self._web_route = web_route

    def parameters_schema(self) -> dict:
        return model_to_json_schema(WebSearchInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = WebSearchInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        mcp_result = None
        if self._web_route is not None:
            mcp_result = await self._web_route(
                kind="search", settings=self._settings, ctx=ctx,
                arguments=inp.model_dump(exclude_none=True), title=f"Search: {inp.query}",
        )
        if mcp_result is not None:
            return mcp_result

        tavily_key = self._get_tavily_key()
        bocha_key = self._get_bocha_key()
        backend_names = (["tavily"] if tavily_key else []) + (["bocha"] if bocha_key else []) + ["duckduckgo", "bing"]
        cache_key = search_cache_key(
            query=inp.query, allowed_domains=inp.allowed_domains,
            blocked_domains=inp.blocked_domains, max_results=inp.max_results,
            backend="+".join(backend_names),
        )
        cached = WEB_TOOL_CACHE.get(cache_key)
        if isinstance(cached, ToolResult):
            return cached_tool_result(cached)

        fallback_errors: list[str] = []
        high: list[tuple[str, Awaitable[list[dict[str, str]]]]] = []
        if tavily_key:
            high.append(("tavily", _search_tavily(inp.query, tavily_key, inp.allowed_domains, inp.blocked_domains, inp.max_results)))
        if bocha_key:
            high.append(("bocha", _search_bocha(inp.query, bocha_key, inp.allowed_domains, inp.blocked_domains, inp.max_results)))
        if high:
            results, used = await self._run_group(high, fallback_errors)
            if results:
                result = self._format_results(inp.query, results, "+".join(used), fallback_errors)
                WEB_TOOL_CACHE.set(cache_key, result, ttl_seconds=600)
                return result

        low = [
            ("duckduckgo", _search_duckduckgo(inp.query, inp.allowed_domains, inp.blocked_domains, inp.max_results)),
            ("bing", _search_bing(inp.query, inp.allowed_domains, inp.blocked_domains, inp.max_results)),
        ]
        results, used = await self._run_group(low, fallback_errors)
        if not results:
            has_timeout = any(err.endswith(": timeout") for err in fallback_errors)
            if has_timeout:
                return ToolResult(
                    output=f"Search timed out for: {inp.query}",
                    metadata=tool_timeout_metadata("web", query=inp.query),
                )
            return ToolResult(
                output=f"No results found for: {inp.query}",
                metadata={"query": inp.query, "results": 0, "backend": "+".join(used) or "none", "fallback_errors": fallback_errors},
            )
        result = self._format_results(inp.query, results, "+".join(used), fallback_errors)
        WEB_TOOL_CACHE.set(cache_key, result, ttl_seconds=600)
        return result

    @staticmethod
    async def _run_group(backends, fallback_errors):
        async def safe(name, coro):
            try:
                return name, await asyncio.wait_for(coro, timeout=15)
            except (asyncio.TimeoutError, httpx.TimeoutException):
                fallback_errors.append(f"{name}: timeout")
                return name, []
            except Exception as exc:
                fallback_errors.append(f"{name}: {exc}")
                return name, []

        gathered = await asyncio.gather(*(safe(name, coro) for name, coro in backends))
        merged: list[dict[str, str]] = []
        used: list[str] = []
        for name, results in gathered:
            if results:
                used.append(name)
                merged.extend(results)
        return merged, used

    def _get_tavily_key(self) -> str | None:
        env_key = os.environ.get("TAVILY_API_KEY")
        if env_key:
            return env_key
        if self._settings:
            return self._settings.get_tavily_api_key()
        return None

    def _get_bocha_key(self) -> str | None:
        env_key = os.environ.get("BOCHA_API_KEY")
        if env_key:
            return env_key
        if self._settings:
            return self._settings.get_bocha_api_key()
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
