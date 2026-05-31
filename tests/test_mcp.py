import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.config import Settings
from voidx.config import McpServerConfig
from voidx.mcp.client import McpClient
from voidx.mcp.manager import McpManager
from voidx.mcp.schema import McpCallResult, McpInitializeParams, McpToolDef
from voidx.mcp.tool import McpToolWrapper
from voidx.permission.service import PermissionService
from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry


def test_initialize_params_serialize_to_mcp_wire_keys():
    payload = McpInitializeParams().to_dict()

    assert payload["protocolVersion"]
    assert payload["clientInfo"]["name"] == "voidx"
    assert "protocol_version" not in payload
    assert "client_info" not in payload


@pytest.mark.asyncio
async def test_mcp_manager_registers_llm_safe_tool_name(tmp_path):
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(
        """
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
                "name": "read/url",
                "description": "Read a URL",
                "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
            }]
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "shutdown":
        break
""",
        encoding="utf-8",
    )
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "mcpServers": {
                "web-reader": {
                    "command": sys.executable,
                    "args": [str(server)],
                },
            },
        }),
        encoding="utf-8",
    )

    settings = Settings(str(tmp_path))
    registry = ToolRegistry(settings=settings)
    manager = McpManager(settings, registry, PermissionService())

    await manager.start_all()
    try:
        mcp_tool_names = [
            tool["function"]["name"]
            for tool in registry.tools_for_llm()
            if tool["function"]["name"].startswith("mcp__")
        ]
        assert len(mcp_tool_names) == 1
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", mcp_tool_names[0])
        assert "/" not in mcp_tool_names[0]

        result = await registry.execute_tool(
            mcp_tool_names[0],
            {"url": "https://example.com"},
            ToolContext(workspace=str(tmp_path)),
        )
        assert result.output == "ok"
        direct = await manager.call_tool("web-reader", "read/url", {"url": "https://example.com"})
        assert direct.content[0]["text"] == "ok"
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_mcp_manager_restart_clears_removed_tools(tmp_path):
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(
        """
import json
import sys

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": message.get("params", {}).get("protocolVersion"), "capabilities": {}}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        result = {"tools": [{"name": "read_url", "inputSchema": {"type": "object", "properties": {}}}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "shutdown":
        break
""",
        encoding="utf-8",
    )
    settings = Settings(str(tmp_path))
    from voidx.config import McpServerConfig

    settings.save_mcp_server(McpServerConfig(
        name="web-reader",
        command=sys.executable,
        args=[str(server)],
    ))
    registry = ToolRegistry(settings=settings)
    manager = McpManager(settings, registry, PermissionService())

    await manager.start_all()
    assert any(tool_id.startswith("mcp__") for tool_id in registry.ids())

    settings.delete_mcp_server("web-reader")
    await manager.restart_all()

    assert not any(tool_id.startswith("mcp__") for tool_id in registry.ids())


@pytest.mark.asyncio
async def test_mcp_manager_restart_after_empty_start_picks_up_new_server(tmp_path):
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(
        """
import json
import sys

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": message.get("params", {}).get("protocolVersion"), "capabilities": {}}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        result = {"tools": [{"name": "read_url", "inputSchema": {"type": "object", "properties": {}}}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "shutdown":
        break
""",
        encoding="utf-8",
    )
    settings = Settings(str(tmp_path))
    registry = ToolRegistry(settings=settings)
    manager = McpManager(settings, registry, PermissionService())

    await manager.start_all()
    assert manager.started is True
    assert not any(tool_id.startswith("mcp__") for tool_id in registry.ids())

    from voidx.config import McpServerConfig

    settings.save_mcp_server(McpServerConfig(
        name="web-reader",
        command=sys.executable,
        args=[str(server)],
    ))
    await manager.restart_all()

    assert any(tool_id.startswith("mcp__") for tool_id in registry.ids())

    await manager.stop_all()


@pytest.mark.asyncio
async def test_mcp_manager_reports_startup_errors(tmp_path):
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "mcpServers": {
                "missing-server": {
                    "command": str(tmp_path / "does-not-exist"),
                },
            },
        }),
        encoding="utf-8",
    )

    settings = Settings(str(tmp_path))
    manager = McpManager(settings, ToolRegistry(settings=settings), PermissionService())

    await manager.start_all()

    status = manager.statuses()[0]
    assert status.name == "missing-server"
    assert status.status == "error"
    assert "Command not found" in status.error_message


@pytest.mark.asyncio
async def test_mcp_tool_wrapper_preserves_non_text_content(tmp_path):
    class FakeClient:
        healthy = True
        status = "connected"
        error_message = ""

        async def call_tool(self, name, arguments):
            return McpCallResult(
                content=[
                    {"type": "image", "mimeType": "image/png", "data": "abcd"},
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "file:///tmp/report.txt",
                            "mimeType": "text/plain",
                            "text": "report body",
                        },
                    },
                ],
                structured_content={"rows": 2},
            )

    wrapper = McpToolWrapper(FakeClient(), McpToolDef(name="snapshot"), "zai/server")

    result = await wrapper.execute({}, ToolContext(workspace=str(tmp_path)))

    assert "image/png" in result.output
    assert "file:///tmp/report.txt" in result.output
    assert "report body" in result.output
    assert '"rows": 2' in result.output


@pytest.mark.asyncio
async def test_builtin_web_mcp_server_lists_tools():
    src_path = str(Path(__file__).parent.parent / "src")
    client = McpClient(McpServerConfig(
        name="voidx-web",
        command=sys.executable,
        args=["-m", "voidx.mcp_servers.web"],
        env={"PYTHONPATH": src_path},
    ))

    await client.start()
    try:
        tools = await client.list_tools()
    finally:
        await client.stop()

    assert [tool.name for tool in tools] == ["web_search", "web_fetch"]
