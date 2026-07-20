"""Tests for MCP server picker (# reference candidates)."""

from __future__ import annotations

from types import SimpleNamespace

from voidx.mcp.schema import McpToolDef
from voidx.ui.tools.mcp_picker import list_mcp_candidates


def _settings_with_servers(servers):
    return SimpleNamespace(list_mcp_servers=lambda: servers)


def _server(name, *, description="", disabled=False, auto=False, tools=None):
    return SimpleNamespace(
        name=name,
        description=description,
        disabled=disabled,
        auto=auto,
        tools=tools,
    )


def test_list_mcp_candidates_returns_configured_servers():
    settings = _settings_with_servers([
        _server("tavily", description="Web search"),
        _server("github", description="GitHub API"),
    ])
    candidates = list_mcp_candidates(".", "", settings=settings)
    assert [c.name for c in candidates] == ["github", "tavily"]


def test_list_mcp_candidates_skips_disabled():
    settings = _settings_with_servers([
        _server("tavily", disabled=True),
        _server("github"),
    ])
    candidates = list_mcp_candidates(".", "", settings=settings)
    assert [c.name for c in candidates] == ["github"]


def test_list_mcp_candidates_filters_by_query():
    settings = _settings_with_servers([
        _server("tavily", description="Web search"),
        _server("github", description="GitHub API"),
    ])
    candidates = list_mcp_candidates(".", "tav", settings=settings)
    assert [c.name for c in candidates] == ["tavily"]


def test_list_mcp_candidates_does_not_expose_catalog_tools():
    settings = _settings_with_servers([_server("tavily")])
    catalog = [
        SimpleNamespace(
            name="tavily",
            tools=(
                McpToolDef(name="tavily_search", description="Search the web"),
                McpToolDef(name="tavily_extract", description="Extract content"),
            ),
            instructions="",
        )
    ]
    candidates = list_mcp_candidates(".", "", settings=settings, catalog=catalog)
    assert candidates[0].description == "(no description)"


def test_list_mcp_candidates_does_not_expose_runtime_instructions():
    settings = _settings_with_servers([_server("tavily")])
    catalog = [
        SimpleNamespace(
            name="tavily",
            tools=(McpToolDef(name="tavily_search"),),
            instructions="Call tavily_search with query and max_results.",
        )
    ]
    candidates = list_mcp_candidates(".", "", settings=settings, catalog=catalog)
    assert candidates[0].description == "(no description)"


def test_list_mcp_candidates_falls_back_when_no_catalog():
    settings = _settings_with_servers([_server("tavily", description="Web search")])
    candidates = list_mcp_candidates(".", "", settings=settings, catalog=None)
    assert candidates[0].description == "Web search"


def test_list_mcp_candidates_empty_description_without_catalog():
    settings = _settings_with_servers([_server("tavily")])
    candidates = list_mcp_candidates(".", "", settings=settings, catalog=None)
    assert candidates[0].description == "(no description)"
