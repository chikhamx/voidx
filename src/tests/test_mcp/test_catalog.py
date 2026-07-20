"""Tests for McpCatalog — in-memory source of truth for discovered MCP tools."""

from voidx.mcp.catalog import McpCatalog
from voidx.mcp.schema import McpToolDef


def _defs(*names: str) -> list[McpToolDef]:
    return [McpToolDef(name=n, description=f"{n} desc") for n in names]


class TestMcpCatalog:
    def test_empty_snapshot(self):
        catalog = McpCatalog()
        assert catalog.snapshot() == []
        assert catalog.tool_def("tavily", "tavily_search") is None
        assert catalog.tool_count("tavily") == 0

    def test_put_and_snapshot(self):
        catalog = McpCatalog()
        catalog.put("tavily", _defs("tavily_search", "tavily_extract"))
        catalog.put("github", _defs("create_issue"))

        entries = {e.name: e for e in catalog.snapshot()}
        assert set(entries) == {"tavily", "github"}
        assert [t.name for t in entries["tavily"].tools] == ["tavily_search", "tavily_extract"]
        assert catalog.tool_count("tavily") == 2
        assert catalog.tool_count("github") == 1

    def test_tool_def_lookup(self):
        catalog = McpCatalog()
        catalog.put("tavily", _defs("tavily_search"))

        found = catalog.tool_def("tavily", "tavily_search")
        assert found is not None
        assert found.description == "tavily_search desc"
        assert catalog.tool_def("tavily", "missing") is None
        assert catalog.tool_def("missing", "tavily_search") is None

    def test_put_replaces_existing_entry(self):
        catalog = McpCatalog()
        catalog.put("tavily", _defs("a", "b"))
        catalog.put("tavily", _defs("c"))

        assert catalog.tool_count("tavily") == 1
        assert catalog.tool_def("tavily", "a") is None
        assert catalog.tool_def("tavily", "c") is not None

    def test_remove(self):
        catalog = McpCatalog()
        catalog.put("tavily", _defs("a"))
        catalog.remove("tavily")

        assert catalog.snapshot() == []
        catalog.remove("tavily")  # idempotent

    def test_clear(self):
        catalog = McpCatalog()
        catalog.put("tavily", _defs("a"))
        catalog.put("github", _defs("b"))
        catalog.clear()

        assert catalog.snapshot() == []
