"""Tests for # MCP server reference candidate RPC method."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidx.ui.gateway.session import GatewaySession
from voidx.ui.output.dock import BottomInputDock
from voidx.ui.protocol.v2.envelope import JsonRpcRequest, JsonRpcResult


def _session(workspace: str, catalog_provider=None) -> GatewaySession:
    dock = BottomInputDock()
    return GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        workspace=workspace,
        mcp_catalog_provider=catalog_provider,
    )


def _write_mcp_config(tmp_path: Path, name: str, description: str = ""):
    (tmp_path / "voidx.json").write_text(
        json.dumps({"mcpServers": {name: {"command": "echo", "description": description}}}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_mcp_candidates_returns_configured_servers(tmp_path: Path):
    _write_mcp_config(tmp_path, "tavily", "Web search")
    session = _session(str(tmp_path))

    result = await session.dispatch_request(
        JsonRpcRequest(id=1, method="mcp.candidates", params={"query": ""})
    )

    assert isinstance(result, JsonRpcResult)
    candidates = result.result["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["name"] == "tavily"
    assert candidates[0]["description"] == "Web search"


@pytest.mark.asyncio
async def test_mcp_candidates_does_not_expose_catalog_instructions(tmp_path: Path):
    _write_mcp_config(tmp_path, "tavily")
    from types import SimpleNamespace
    catalog = [
        SimpleNamespace(
            name="tavily",
            tools=(),
            server_info={"name": "tavily-mcp", "version": "1.0"},
            instructions="Call search with query and max_results.",
        )
    ]
    session = _session(str(tmp_path), catalog_provider=lambda: catalog)

    result = await session.dispatch_request(
        JsonRpcRequest(id=2, method="mcp.candidates", params={"query": ""})
    )

    assert isinstance(result, JsonRpcResult)
    candidates = result.result["candidates"]
    assert candidates[0]["description"] == "(no description)"


@pytest.mark.asyncio
async def test_mcp_candidates_does_not_expose_tool_list(tmp_path: Path):
    _write_mcp_config(tmp_path, "tavily")
    from voidx.mcp.schema import McpToolDef
    from types import SimpleNamespace
    catalog = [
        SimpleNamespace(
            name="tavily",
            tools=(McpToolDef(name="search"), McpToolDef(name="extract")),
            server_info={},
            instructions="",
        )
    ]
    session = _session(str(tmp_path), catalog_provider=lambda: catalog)

    result = await session.dispatch_request(
        JsonRpcRequest(id=3, method="mcp.candidates", params={"query": ""})
    )

    assert isinstance(result, JsonRpcResult)
    assert result.result["candidates"][0]["description"] == "(no description)"
