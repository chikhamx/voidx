"""MCP current-turn context rendering and schema summarization."""

from voidx.mcp.context import (
    MCP_TOOL_CONTEXT_MARKER,
    MCP_TOOL_CONTEXT_STRIPPED_MARKER,
    render_mcp_tool_context,
    strip_mcp_tool_context,
)
from voidx.mcp.schema import McpToolDef
from voidx.mcp.schema_summary import summarize_schema


_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query"},
        "max_results": {"type": "integer", "description": "Max results"},
        "search_depth": {"type": "string", "enum": ["basic", "advanced"]},
    },
    "required": ["query"],
    "additionalProperties": False,
}


def _tool(name: str, description: str = "", schema: dict | None = None) -> McpToolDef:
    return McpToolDef(name=name, description=description, inputSchema=schema or {})


class TestSummarizeSchema:
    def test_required_and_optional_split(self):
        summary = summarize_schema(_SEARCH_SCHEMA)
        by_name = {f.name: f for f in summary.fields}
        assert by_name["query"].required is True
        assert by_name["max_results"].required is False
        assert by_name["query"].type == "string"
        assert by_name["query"].description == "Search query"
        assert by_name["search_depth"].enum == ["basic", "advanced"]
        assert summary.truncated is False

    def test_required_first_ordering(self):
        summary = summarize_schema(_SEARCH_SCHEMA)
        assert summary.fields[0].name == "query"

    def test_truncation(self):
        schema = {
            "type": "object",
            "properties": {f"field_{i}": {"type": "string"} for i in range(20)},
        }
        summary = summarize_schema(schema, max_fields=5)
        assert len(summary.fields) == 5
        assert summary.truncated is True

    def test_nested_object_and_array_types(self):
        schema = {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}},
                "options": {"type": "object"},
            },
        }
        summary = summarize_schema(schema)
        by_name = {f.name: f for f in summary.fields}
        assert by_name["urls"].type == "array[string]"
        assert by_name["options"].type == "object"

    def test_empty_schema(self):
        summary = summarize_schema({})
        assert list(summary.fields) == []
        assert summary.truncated is False


class TestRenderMcpToolContext:
    def test_server_context_has_marker_and_tool_lines(self):
        tools = [
            _tool("tavily_search", "Search the web.", _SEARCH_SCHEMA),
            _tool("tavily_extract", "Extract page content."),
        ]
        text = render_mcp_tool_context("tavily", "connected", tools)

        assert text.startswith(f"{MCP_TOOL_CONTEXT_MARKER}\nScope: current-turn")
        assert "## MCP Server: tavily" in text
        assert "Status: connected" in text
        assert "- tavily_search: Search the web." in text
        assert "Required: query" in text
        assert "Optional: max_results, search_depth" in text
        assert 'mcp(op="call", server="tavily", tool="tavily_search"' in text
        assert "- tavily_extract: Extract page content." in text

    def test_example_arguments_cover_required_fields(self):
        tools = [_tool("tavily_search", schema=_SEARCH_SCHEMA)]
        text = render_mcp_tool_context("tavily", "connected", tools)
        assert 'arguments={"query":' in text
        assert 'arguments="{' not in text

    def test_empty_tool_list(self):
        text = render_mcp_tool_context("empty", "connected", [])
        assert "No tools available" in text


class TestStripMcpToolContext:
    def test_strip_replaces_with_summary(self):
        text = render_mcp_tool_context(
            "tavily", "connected", [_tool("tavily_search"), _tool("tavily_extract")],
        )
        stripped = strip_mcp_tool_context(text)

        assert MCP_TOOL_CONTEXT_STRIPPED_MARKER in stripped
        assert "tavily" in stripped
        assert "tavily_search" in stripped
        assert "Scope: current-turn" not in stripped

    def test_strip_preserves_prefix(self):
        text = "some output\n\n" + render_mcp_tool_context("tavily", "connected", [_tool("a")])
        stripped = strip_mcp_tool_context(text)
        assert stripped.startswith("some output")

    def test_strip_list_content(self):
        text = render_mcp_tool_context("tavily", "connected", [_tool("a")])
        content = [{"type": "text", "text": text}, {"type": "image", "data": "x"}]
        stripped = strip_mcp_tool_context(content)

        assert stripped[1] == {"type": "image", "data": "x"}

    def test_strip_noop_without_marker(self):
        assert strip_mcp_tool_context("hello") == "hello"
