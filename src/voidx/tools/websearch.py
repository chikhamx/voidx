"""WebSearch tool — search the web. Claude Code aligned."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult


class WebSearchInput(BaseModel):
    query: str = Field(description="Search query, max 70 characters")
    domain_filter: str | None = Field(default=None, description="Limit results to this domain (e.g. 'docs.python.org')")


class WebSearchTool(BaseTool):
    id = "websearch"
    description = "Search the web. Returns titles, URLs, and snippets."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(WebSearchInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = WebSearchInput.model_validate(args)

        # DuckDuckGo HTML search as a free, no-API-key option
        search_url = "https://html.duckduckgo.com/html/"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    search_url,
                    data={"q": inp.query},
                    headers={"User-Agent": "voidx/0.1"},
                )
                resp.raise_for_status()
        except Exception as e:
            return ToolResult(
                output=f"Search failed: {e}. Query: {inp.query}",
                metadata={"query": inp.query, "error": str(e)},
            )

        # Simple HTML result extraction
        import re
        results = []
        # Match result snippets from DuckDuckGo HTML page
        for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            resp.text,
            re.DOTALL,
        ):
            url = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            snippet = re.sub(r"<[^>]+>", "", m.group(3)).strip()
            if inp.domain_filter and inp.domain_filter not in url:
                continue
            results.append(f"- [{title}]({url})\n  {snippet}")
            if len(results) >= 10:
                break

        if not results:
            return ToolResult(
                output=f"No results found for: {inp.query}",
                metadata={"query": inp.query, "results": 0},
            )

        return ToolResult(
            title=f"Search: {inp.query}",
            output="\n\n".join(results),
            metadata={"query": inp.query, "results": len(results)},
        )
