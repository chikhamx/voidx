"""McpGatewayTool — fixed `mcp` tool exposing list/load/call over McpCatalog."""

import pytest

from voidx.mcp.catalog import McpCatalog
from voidx.tooling.adapters.mcp import McpGatewayTool
from voidx.mcp.schema import McpCallResult, McpRuntimeStatus, McpToolDef
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext


_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query"},
        "max_results": {"type": "integer"},
    },
    "required": ["query"],
    "additionalProperties": False,
}


class StubManager:
    def __init__(self) -> None:
        self.catalog = McpCatalog()
        self._statuses: list[McpRuntimeStatus] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.call_result = McpCallResult(content=[{"type": "text", "text": "ok"}])

    def add_server(self, name: str, status: str, tools: list[McpToolDef], error: str = "") -> None:
        self.catalog.put(name, tools)
        self._statuses.append(McpRuntimeStatus(name=name, status=status, tool_count=len(tools), error_message=error))

    def server_config(self, server: str):
        return getattr(self, "configs", {}).get(server)

    def catalog_snapshot(self):
        return self.catalog.snapshot()

    def tool_def(self, server: str, tool: str):
        return self.catalog.tool_def(server, tool)

    def statuses(self):
        return list(self._statuses)

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        return self.call_result


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(workspace=str(tmp_path))


@pytest.fixture
def manager() -> StubManager:
    stub = StubManager()
    stub.add_server("tavily", "connected", [
        McpToolDef(name="tavily_search", description="Search the web.", inputSchema=_SEARCH_SCHEMA),
        McpToolDef(name="tavily_extract", description="Extract page content."),
    ])
    stub.add_server("github", "connected", [
        McpToolDef(name="create_issue", description="Create an issue."),
    ])
    stub.add_server("broken", "error", [], error="connection refused")
    return stub


class TestList:
    @pytest.mark.asyncio
    async def test_lists_servers_with_status_and_counts(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute({"op": "list"}, _ctx(tmp_path))

        assert not result.metadata.get("error")
        assert "tavily" in result.output
        assert "connected" in result.output
        assert "2 tools" in result.output
        assert "broken" in result.output
        assert "error" in result.output


    @pytest.mark.asyncio
    async def test_list_uses_semantic_server_summary_and_defers_tools_to_load(
        self, manager, tmp_path,
    ):
        from types import SimpleNamespace

        manager.configs = {
            "tavily": SimpleNamespace(description="Web research", source="workspace"),
        }
        result = await McpGatewayTool(manager).execute({"op": "list"}, _ctx(tmp_path))

        assert "tavily: Web research" in result.output
        assert 'mcp(op="load", server="tavily")' in result.output
        assert "tavily_search" not in result.output
        assert "tavily_extract" not in result.output


    @pytest.mark.asyncio
    async def test_list_does_not_expose_runtime_instructions(self, manager, tmp_path):
        tools = manager.catalog.snapshot()[0].tools
        manager.catalog.put(
            "tavily",
            tools,
            instructions="Call tavily_search with query and max_results.",
        )

        result = await McpGatewayTool(manager).execute({"op": "list"}, _ctx(tmp_path))

        assert "Call tavily_search" not in result.output
        assert "max_results" not in result.output
        assert 'mcp(op="load", server="tavily")' in result.output

    @pytest.mark.asyncio
    async def test_list_uses_generated_catalog_description(self, manager, tmp_path):
        tools = manager.catalog.snapshot()[0].tools
        manager.catalog.put(
            "tavily",
            tools,
            description="Search the web for current information.",
        )

        result = await McpGatewayTool(manager).execute({"op": "list"}, _ctx(tmp_path))

        assert "tavily: Search the web for current information." in result.output
    @pytest.mark.asyncio
    async def test_query_filters_by_server_or_tool(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute({"op": "list", "query": "issue"}, _ctx(tmp_path))

        assert "github" in result.output
        assert "tavily" not in result.output

    @pytest.mark.asyncio
    async def test_query_matches_configuration_description(self, manager, tmp_path):
        from types import SimpleNamespace

        manager.configs = {
            "tavily": SimpleNamespace(description="Web research", source="workspace"),
        }
        result = await McpGatewayTool(manager).execute(
            {"op": "list", "query": "research"},
            _ctx(tmp_path),
        )

        assert "tavily" in result.output
        assert "Web research" in result.output
        assert "github" not in result.output

    @pytest.mark.asyncio
    async def test_query_no_match(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute({"op": "list", "query": "nonexistent"}, _ctx(tmp_path))
        assert "No MCP servers match" in result.output

    @pytest.mark.asyncio
    async def test_no_servers_configured(self, tmp_path):
        tool = McpGatewayTool(StubManager())
        result = await tool.execute({"op": "list"}, _ctx(tmp_path))
        assert "No MCP servers" in result.output


class TestLoad:
    @pytest.mark.asyncio
    async def test_load_server_renders_context_with_marker(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute({"op": "load", "server": "tavily"}, _ctx(tmp_path))

        assert not result.metadata.get("error")
        assert result.output.startswith("VOIDX_MCP_TOOL_CONTEXT")
        assert "## MCP Server: tavily" in result.output
        assert "tavily_search" in result.output
        assert "Required: query" in result.output
        assert result.metadata["server"] == "tavily"
        assert result.metadata["tool_names"] == ["tavily_search", "tavily_extract"]
        assert result.metadata["schema_hash"]
        assert result.metadata["truncated"] is False

    @pytest.mark.asyncio
    async def test_load_single_tool(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute(
            {"op": "load", "server": "tavily", "tool": "tavily_search"}, _ctx(tmp_path),
        )

        assert not result.metadata.get("error")
        assert "tavily_search" in result.output
        assert "tavily_extract" not in result.output
        assert result.metadata["tool_names"] == ["tavily_search"]

    @pytest.mark.asyncio
    async def test_load_unknown_server_suggests_candidates(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute({"op": "load", "server": "gitlab"}, _ctx(tmp_path))

        assert result.metadata.get("error")
        assert "gitlab" in result.output
        assert "tavily" in result.output  # candidates listed
        assert 'mcp(op="list")' in result.output

    @pytest.mark.asyncio
    async def test_load_unknown_tool(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute(
            {"op": "load", "server": "tavily", "tool": "nope"}, _ctx(tmp_path),
        )

        assert result.metadata.get("error")
        assert "nope" in result.output
        assert "tavily_search" in result.output

    @pytest.mark.asyncio
    async def test_load_requires_server(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute({"op": "load"}, _ctx(tmp_path))
        assert result.metadata.get("error")


class TestCall:
    @pytest.mark.asyncio
    async def test_call_executes_real_tool(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute(
            {"op": "call", "server": "tavily", "tool": "tavily_search", "arguments": {"query": "x"}},
            _ctx(tmp_path),
        )

        assert not result.metadata.get("error")
        assert result.output == "ok"
        assert manager.calls == [("tavily", "tavily_search", {"query": "x"})]
        assert result.metadata["server"] == "tavily"
        assert result.metadata["tool"] == "tavily_search"

    def test_parameters_schema_exposes_object_arguments_only(self, manager):
        schema = McpGatewayTool(manager).parameters_schema()
        arguments = schema["properties"]["arguments"]

        assert arguments["type"] == ["object", "null"]
        assert arguments["additionalProperties"] is True

    @pytest.mark.asyncio
    async def test_call_rejects_json_string_arguments(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute(
            {"op": "call", "server": "tavily", "tool": "tavily_search", "arguments": '{"query": "x"}'},
            _ctx(tmp_path),
        )

        assert result.metadata.get("error")
        assert "arguments must be a JSON object" in result.output
        assert manager.calls == []

    @pytest.mark.asyncio
    async def test_call_schema_error_names_field(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute(
            {"op": "call", "server": "tavily", "tool": "tavily_search", "arguments": {}},
            _ctx(tmp_path),
        )

        assert result.metadata.get("error")
        assert result.metadata["error_kind"] == "schema"
        assert "query" in result.output
        assert 'mcp(op="load", server="tavily", tool="tavily_search")' in result.output
        assert manager.calls == []

    @pytest.mark.asyncio
    async def test_call_unknown_server(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute(
            {"op": "call", "server": "gitlab", "tool": "x", "arguments": {}}, _ctx(tmp_path),
        )
        assert result.metadata.get("error")
        assert "gitlab" in result.output
        assert 'mcp(op="list")' in result.output

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute(
            {"op": "call", "server": "tavily", "tool": "nope", "arguments": {}}, _ctx(tmp_path),
        )
        assert result.metadata.get("error")
        assert "nope" in result.output
        assert "tavily_search" in result.output

    @pytest.mark.asyncio
    async def test_call_error_server_reports_status(self, manager, tmp_path):
        tool = McpGatewayTool(manager)
        result = await tool.execute(
            {"op": "call", "server": "broken", "tool": "x", "arguments": {}}, _ctx(tmp_path),
        )
        assert result.metadata.get("error")
        assert "broken" in result.output
        assert "error" in result.output

    @pytest.mark.asyncio
    async def test_call_mcp_error_result_preserves_flag(self, manager, tmp_path):
        manager.call_result = McpCallResult(content=[{"type": "text", "text": "boom"}], isError=True)
        tool = McpGatewayTool(manager)
        result = await tool.execute(
            {"op": "call", "server": "tavily", "tool": "tavily_search", "arguments": {"query": "x"}},
            _ctx(tmp_path),
        )
        assert result.metadata.get("error")
        assert "boom" in result.output


class TestDescription:
    def test_description_teaches_workflow_without_catalog(self, manager):
        tool = McpGatewayTool(manager)
        desc = tool.description
        assert 'op="list"' in desc
        assert 'op="load"' in desc
        assert 'op="call"' in desc
        assert "tavily" not in desc
        assert "github" not in desc
