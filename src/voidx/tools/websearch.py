"""WebSearch tool — search the web. Tavily > DuckDuckGo fallback."""

from __future__ import annotations

import os
from html.parser import HTMLParser

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult


class WebSearchInput(BaseModel):
    query: str = Field(description="Search query, max 70 characters")
    domain_filter: str | None = Field(default=None, description="Limit results to this domain (e.g. 'docs.python.org')")


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
        pass
    return parser.results()


# ── Tavily API backend ──────────────────────────────────────────────────

async def _search_tavily(query: str, api_key: str, domain_filter: str | None = None) -> list[dict[str, str]]:
    """Search via Tavily API. Returns list of {url, title, snippet}."""
    import httpx

    url = "https://api.tavily.com/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "query": query,
        "max_results": 10,
        "include_answer": False,
        "search_depth": "basic",
    }
    if domain_filter:
        payload["include_domains"] = [domain_filter]

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

async def _search_duckduckgo(query: str, domain_filter: str | None = None) -> list[dict[str, str]]:
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

    if domain_filter:
        results = [r for r in results if domain_filter in r["url"]]

    return results[:10]


# ── Tool ────────────────────────────────────────────────────────────────

class WebSearchTool(BaseTool):
    id = "websearch"
    description = "Search the web. Returns titles, URLs, and snippets."

    def __init__(self, settings=None):
        self._settings = settings

    def parameters_schema(self) -> dict:
        return model_to_json_schema(WebSearchInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = WebSearchInput.model_validate(args)

        # Try Tavily first
        tavily_key = self._get_tavily_key()
        if tavily_key:
            try:
                results = await _search_tavily(inp.query, tavily_key, inp.domain_filter)
                if results:
                    return self._format_results(inp.query, results[:10], "tavily")
            except Exception:
                pass  # fall through to DuckDuckGo

        # DuckDuckGo fallback
        try:
            results = await _search_duckduckgo(inp.query, inp.domain_filter)
        except Exception as e:
            return ToolResult(
                output=f"Search failed: {e}. Query: {inp.query}",
                metadata={"query": inp.query, "error": str(e)},
            )

        if not results:
            return ToolResult(
                output=f"No results found for: {inp.query}",
                metadata={"query": inp.query, "results": 0},
            )

        return self._format_results(inp.query, results[:10], "duckduckgo")

    def _get_tavily_key(self) -> str | None:
        env_key = os.environ.get("TAVILY_API_KEY")
        if env_key:
            return env_key
        if self._settings:
            return self._settings.get_tavily_api_key()
        return None

    @staticmethod
    def _format_results(query: str, results: list[dict[str, str]], backend: str) -> ToolResult:
        formatted = []
        for r in results:
            formatted.append(f"- [{r['title']}]({r['url']})\n  {r['snippet']}")
        return ToolResult(
            title=f"Search: {query}",
            output="\n\n".join(formatted),
            metadata={"query": query, "results": len(results), "backend": backend},
        )
