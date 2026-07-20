"""McpManager exposes discovered tools through a single McpCatalog snapshot."""

import json
import sys

import pytest

from voidx.config import Settings
from voidx.mcp.manager import McpManager
from voidx.permission.service import PermissionService
from voidx.tools.registry import ToolRegistry


_FAKE_SERVER = """
import json
import sys

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    if method == "initialize":
        params = message.get("params", {})
        result = {
            "protocolVersion": params.get("protocolVersion"),
            "capabilities": {},
            "serverInfo": {"name": "fake", "version": "1.0.0"},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": "search", "description": "Search", "inputSchema": {"type": "object"}},
                {"name": "blocked", "description": "Blocked", "inputSchema": {"type": "object"}},
            ]
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "shutdown":
        break
"""


def _write_env(tmp_path, tools_field=None):
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(_FAKE_SERVER, encoding="utf-8")
    config: dict = {"command": sys.executable, "args": [str(server)]}
    if tools_field is not None:
        config["tools"] = tools_field
    (tmp_path / "voidx.json").write_text(
        json.dumps({"mcpServers": {"fake": config}}),
        encoding="utf-8",
    )
    return Settings(str(tmp_path))


@pytest.mark.asyncio
async def test_catalog_snapshot_reflects_discovered_tools(tmp_path):
    settings = _write_env(tmp_path)
    manager = McpManager(settings, ToolRegistry(settings=settings), PermissionService())

    await manager.start_all()
    await manager.wait_ready()
    try:
        entries = {e.name: e for e in manager.catalog_snapshot()}
        assert set(entries) == {"fake"}
        assert [t.name for t in entries["fake"].tools] == ["search", "blocked"]
        assert manager.tool_def("fake", "search").description == "Search"
        assert manager.tool_def("fake", "missing") is None
    finally:
        await manager.stop_all()

    assert manager.catalog_snapshot() == []
    assert manager.tool_def("fake", "search") is None


@pytest.mark.asyncio
async def test_catalog_only_contains_config_allowed_tools(tmp_path):
    settings = _write_env(tmp_path, tools_field=["search"])
    manager = McpManager(settings, ToolRegistry(settings=settings), PermissionService())

    await manager.start_all()
    await manager.wait_ready()
    try:
        entries = {e.name: e for e in manager.catalog_snapshot()}
        assert [t.name for t in entries["fake"].tools] == ["search"]
        assert manager.tool_def("fake", "blocked") is None
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_config_denied_tool_maps_to_gateway_resource(tmp_path):
    from voidx.permission.engine import authorize_tool_call

    settings = _write_env(tmp_path, tools_field={"search": True, "blocked": False})
    permission = PermissionService()
    manager = McpManager(settings, ToolRegistry(settings=settings), permission)

    await manager.start_all()
    await manager.wait_ready()
    try:
        context = permission._context(workspace=str(tmp_path))
        denied = authorize_tool_call(
            {"name": "mcp", "args": {"op": "call", "server": "fake", "tool": "blocked"}},
            context,
        )
        assert denied.action == "deny"

        not_denied = authorize_tool_call(
            {"name": "mcp", "args": {"op": "call", "server": "fake", "tool": "search"}},
            context,
        )
        assert not_denied.action == "ask"
    finally:
        await manager.stop_all()
