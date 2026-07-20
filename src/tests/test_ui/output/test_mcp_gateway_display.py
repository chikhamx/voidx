"""Gateway `mcp` tool calls render as real MCP actions, not `Mcp("call")`."""

from voidx.ui.output.dock.nodes import _tool_header
from voidx.ui.output.tool_display import (
    extract_tool_display_value,
    mcp_gateway_tool_name,
)


class TestGatewayToolName:
    def test_call_renders_server_and_tool(self):
        name = mcp_gateway_tool_name({"op": "call", "server": "tavily", "tool": "tavily_search"})
        assert name == "Tavily Search"

    def test_call_dedupes_server_prefix_in_tool(self):
        name = mcp_gateway_tool_name({"op": "call", "server": "github", "tool": "github_create_issue"})
        assert name == "Github Create Issue"

    def test_list(self):
        assert mcp_gateway_tool_name({"op": "list"}) == "MCP List"

    def test_load(self):
        assert mcp_gateway_tool_name({"op": "load", "server": "github"}) == "MCP Load"

    def test_unknown_op_falls_back(self):
        assert mcp_gateway_tool_name({}) == "MCP"


class TestGatewayDisplayValue:
    def test_call_extracts_query_from_arguments_string(self):
        raw_args = {
            "op": "call",
            "server": "tavily",
            "tool": "tavily_search",
            "arguments": '{"query": "goal mode mechanism", "max_results": 3}',
        }
        value = extract_tool_display_value("mcp", raw_args, "")
        assert value == "goal mode mechanism"

    def test_call_extracts_url_list_with_count(self):
        raw_args = {
            "op": "call",
            "server": "tavily",
            "tool": "tavily_extract",
            "arguments": '{"urls": ["https://a.com", "https://b.com", "https://c.com"]}',
        }
        value = extract_tool_display_value("mcp", raw_args, "")
        assert value == "https://a.com +2 more"

    def test_call_accepts_dict_arguments(self):
        raw_args = {"op": "call", "server": "tavily", "tool": "tavily_search", "arguments": {"query": "x"}}
        assert extract_tool_display_value("mcp", raw_args, "") == "x"

    def test_call_parse_failure_falls_back_to_server_tool(self):
        raw_args = {"op": "call", "server": "tavily", "tool": "tavily_search", "arguments": "{bad"}
        value = extract_tool_display_value("mcp", raw_args, "")
        assert value == "tavily/tavily_search"

    def test_load_value_is_server(self):
        assert extract_tool_display_value("mcp", {"op": "load", "server": "github"}, "") == "github"


class TestToolHeader:
    def test_header_renders_real_action(self):
        header = _tool_header(
            "mcp",
            "Calling MCP tool",
            "",
            {"op": "call", "server": "tavily", "tool": "tavily_search", "arguments": '{"query": "x"}'},
        )
        assert "Tavily Search" in header
        assert "[cyan]x[/cyan]" in header

    def test_header_for_load(self):
        header = _tool_header("mcp", "", "", {"op": "load", "server": "github"})
        assert "MCP Load" in header
        assert "github" in header
