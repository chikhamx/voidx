"""mcp.exposure setting: direct (default) | gateway | hybrid."""

import json
import sys

import pytest

from voidx.config import Settings
from voidx.mcp.manager import McpManager
from voidx.permission.service import PermissionService
from voidx.tools.base import ToolContext
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
            "tools": [{
                "name": "search",
                "description": "Search",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }]
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "shutdown":
        break
"""


def _write_env(tmp_path, extra: dict | None = None):
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(_FAKE_SERVER, encoding="utf-8")
    config = {
        "mcpServers": {"fake": {"command": sys.executable, "args": [str(server)]}},
        **(extra or {}),
    }
    (tmp_path / "voidx.json").write_text(json.dumps(config), encoding="utf-8")
    return Settings(str(tmp_path))


class TestExposureSetting:
    def test_default_is_direct(self, tmp_path):
        settings = _write_env(tmp_path)
        assert settings.get_mcp_exposure() == "direct"

    def test_explicit_gateway(self, tmp_path):
        settings = _write_env(tmp_path, {"mcp": {"exposure": "gateway"}})
        assert settings.get_mcp_exposure() == "gateway"

    def test_explicit_hybrid(self, tmp_path):
        settings = _write_env(tmp_path, {"mcp": {"exposure": "hybrid"}})
        assert settings.get_mcp_exposure() == "hybrid"

    def test_invalid_value_falls_back_to_direct(self, tmp_path):
        settings = _write_env(tmp_path, {"mcp": {"exposure": "banana"}})
        assert settings.get_mcp_exposure() == "direct"


class TestGatewayMode:
    @pytest.mark.asyncio
    async def test_gateway_mode_skips_direct_registration_but_keeps_catalog(self, tmp_path):
        settings = _write_env(tmp_path, {"mcp": {"exposure": "gateway"}})
        registry = ToolRegistry(settings=settings)
        manager = McpManager(settings, registry, PermissionService())

        await manager.start_all()
        await manager.wait_ready()
        try:
            tool_names = [t["function"]["name"] for t in registry.tools_for_llm()]
            assert not any(name.startswith("mcp__") for name in tool_names)

            entries = {e.name: e for e in manager.catalog_snapshot()}
            assert [t.name for t in entries["fake"].tools] == ["search"]
        finally:
            await manager.stop_all()


class TestAutoExposure:
    @pytest.mark.asyncio
    async def test_auto_server_uses_gateway_without_direct_tool_registration(self, tmp_path):
        settings = _write_env(tmp_path, {
            "mcpServers": {
                "fake": {
                    "command": sys.executable,
                    "args": [str(tmp_path / "fake_mcp_server.py")],
                    "auto": True,
                }
            }
        })
        registry = ToolRegistry(settings=settings)
        manager = McpManager(settings, registry, PermissionService())

        await manager.start_all()
        await manager.wait_ready()
        try:
            tool_names = [t["function"]["name"] for t in registry.tools_for_llm()]
            assert not any(name.startswith("mcp__") for name in tool_names)
            assert manager.catalog_snapshot()[0].name == "fake"
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_direct_mode_registers_wrappers(self, tmp_path):
        settings = _write_env(tmp_path)
        registry = ToolRegistry(settings=settings)
        manager = McpManager(settings, registry, PermissionService())

        await manager.start_all()
        await manager.wait_ready()
        try:
            tool_names = [t["function"]["name"] for t in registry.tools_for_llm()]
            assert any(name.startswith("mcp__") for name in tool_names)
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_hybrid_mode_registers_wrappers(self, tmp_path):
        settings = _write_env(tmp_path, {"mcp": {"exposure": "hybrid"}})
        registry = ToolRegistry(settings=settings)
        manager = McpManager(settings, registry, PermissionService())

        await manager.start_all()
        await manager.wait_ready()
        try:
            tool_names = [t["function"]["name"] for t in registry.tools_for_llm()]
            assert any(name.startswith("mcp__") for name in tool_names)
        finally:
            await manager.stop_all()


class TestGatewayEndToEnd:
    @pytest.mark.asyncio
    async def test_gateway_mode_full_call_flow(self, tmp_path):
        from voidx.mcp.gateway import McpGatewayTool

        settings = _write_env(tmp_path, {"mcp": {"exposure": "gateway"}})
        registry = ToolRegistry(settings=settings)
        manager = McpManager(settings, registry, PermissionService())
        gateway = McpGatewayTool(manager)
        registry.register(gateway.id, gateway, gateway.description, gateway.parameters_schema())

        await manager.start_all()
        await manager.wait_ready()
        try:
            ctx = ToolContext(workspace=str(tmp_path))

            listed = await registry.execute_tool("mcp", {"op": "list"}, ctx)
            assert "fake" in listed.output
            assert "connected" in listed.output

            loaded = await registry.execute_tool("mcp", {"op": "load", "server": "fake"}, ctx)
            assert loaded.output.startswith("VOIDX_MCP_TOOL_CONTEXT")
            assert "search" in loaded.output

            called = await registry.execute_tool(
                "mcp",
                {"op": "call", "server": "fake", "tool": "search", "arguments": {"query": "x"}},
                ctx,
            )
            assert called.output == "ok"
            assert not called.metadata.get("error")
        finally:
            await manager.stop_all()
