"""MCP server reference resolution for $name tokens in user messages."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voidx.agent.adapters.mcp.references import mcp_reference_message
from voidx.mcp.schema import McpToolDef
from voidx.mcp.context import MCP_TOOL_CONTEXT_MARKER


def _fake_manager(server: str, tools: list[McpToolDef], status: str = "connected"):
    manager = SimpleNamespace()
    manager.statuses = lambda: [
        SimpleNamespace(name=server, status=status, tool_count=len(tools), error_message="")
    ]
    manager.list_tools_for_server = AsyncMock(return_value=tools)
    return manager


@pytest.mark.asyncio
async def test_mcp_reference_injects_tool_context_for_matched_server():
    tools = [McpToolDef(name="search", description="Search the web")]
    manager = _fake_manager("tavily", tools)

    result = await mcp_reference_message(
        "search using $tavily please",
        settings=SimpleNamespace(list_mcp_servers=lambda: [
            SimpleNamespace(name="tavily", disabled=False, auto=False),
        ]),
        manager=manager,
    )

    assert MCP_TOOL_CONTEXT_MARKER in result.prefix
    assert 'mcp(op="load", server="tavily")' in result.prefix
    assert "Tools:" not in result.prefix
    assert len(result.remove_spans) == 1
    assert result.servers == ["tavily"]


@pytest.mark.asyncio
async def test_mcp_reference_uses_semantic_summary_for_load_prompt():
    tools = [McpToolDef(name="search", description="Search the web")]
    manager = _fake_manager("tavily", tools)

    result = await mcp_reference_message(
        "use $tavily",
        settings=SimpleNamespace(list_mcp_servers=lambda: [
            SimpleNamespace(
                name="tavily",
                disabled=False,
                auto=True,
                description="Web search",
                source="builtin",
            ),
        ]),
        manager=manager,
    )

    assert MCP_TOOL_CONTEXT_MARKER in result.prefix
    assert "Tools:" not in result.prefix
    assert 'mcp(op="load", server="tavily")' in result.prefix
    assert "Web search" in result.prefix


@pytest.mark.asyncio
async def test_mcp_reference_uses_manager_generated_description():
    manager = _fake_manager("tavily", [McpToolDef(name="search")])
    manager.server_description = lambda name: "Search the web for current information."

    result = await mcp_reference_message(
        "use $tavily",
        settings=SimpleNamespace(list_mcp_servers=lambda: [
            SimpleNamespace(name="tavily", disabled=False, auto=True, description=""),
        ]),
        manager=manager,
    )

    assert "Search the web for current information." in result.prefix


@pytest.mark.asyncio
async def test_manual_mcp_reference_is_summary_and_requires_load():
    manager = _fake_manager("typex", [McpToolDef(name="send_message")])

    result = await mcp_reference_message(
        "use $typex",
        settings=SimpleNamespace(list_mcp_servers=lambda: [
            SimpleNamespace(
                name="typex",
                disabled=False,
                auto=False,
                description="Team chat and messaging",
                source="local",
            ),
        ]),
        manager=manager,
    )

    assert "Team chat and messaging" in result.prefix
    assert 'mcp(op="load", server="typex")' in result.prefix
    assert "send_message" not in result.prefix
    assert result.servers == ["typex"]



@pytest.mark.asyncio
async def test_mcp_reference_does_not_expose_runtime_instructions():
    manager = _fake_manager("tavily", [McpToolDef(name="search")])
    manager.catalog_snapshot = lambda: [
        SimpleNamespace(
            name="tavily",
            instructions="Call search with query and max_results.",
            server_info={"name": "tavily-mcp", "version": "1.0"},
        )
    ]

    result = await mcp_reference_message(
        "use $tavily",
        settings=SimpleNamespace(list_mcp_servers=lambda: [
            SimpleNamespace(
                name="tavily",
                disabled=False,
                auto=True,
                description="Web research",
            ),
        ]),
        manager=manager,
    )

    assert "Web research" in result.prefix
    assert "Call search" not in result.prefix
    assert "max_results" not in result.prefix
    assert "Server-Info:" not in result.prefix


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["connecting", "disconnected", "error"])
async def test_mcp_reference_summary_omits_runtime_status(status):
    manager = _fake_manager("tavily", [], status=status)

    result = await mcp_reference_message(
        "use $tavily",
        settings=SimpleNamespace(list_mcp_servers=lambda: [
            SimpleNamespace(
                name="tavily",
                disabled=False,
                auto=False,
                description="Web research",
            ),
        ]),
        manager=manager,
    )

    assert "Web research" in result.prefix
    assert "Status:" not in result.prefix
    assert result.remove_spans
    assert result.servers == ["tavily"]

@pytest.mark.asyncio
async def test_mcp_reference_returns_empty_when_no_dollar_sign():
    result = await mcp_reference_message(
        "no references here",
        settings=SimpleNamespace(list_mcp_servers=lambda: []),
        manager=None,
    )
    assert result.prefix == ""
    assert result.remove_spans == []


@pytest.mark.asyncio
async def test_mcp_reference_skips_unknown_names():
    tools = [McpToolDef(name="search")]
    manager = _fake_manager("tavily", tools)

    result = await mcp_reference_message(
        "use $unknown please",
        settings=SimpleNamespace(list_mcp_servers=lambda: [
            SimpleNamespace(name="tavily", disabled=False, auto=False),
        ]),
        manager=manager,
    )

    assert result.prefix == ""
    assert result.servers == []


@pytest.mark.asyncio
async def test_mcp_reference_skips_disabled_servers():
    manager = _fake_manager("tavily", [McpToolDef(name="search")])

    result = await mcp_reference_message(
        "use $tavily",
        settings=SimpleNamespace(list_mcp_servers=lambda: [
            SimpleNamespace(name="tavily", disabled=True, auto=False),
        ]),
        manager=manager,
    )

    assert result.prefix == ""
    assert result.servers == []
