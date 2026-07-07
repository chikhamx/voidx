import json
import re
import sys
from pathlib import Path

import pytest


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
    await manager.wait_ready()
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
async def test_call_tool_sends_empty_arguments_object(tmp_path):
    """call_tool({}) must send 'arguments': {} — not omit the field."""
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
                "name": "get_me",
                "description": "Get current user",
                "inputSchema": {"type": "object", "properties": {}},
            }]
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/call":
        # Echo back the params so the test can inspect them
        params = message.get("params", {})
        has_args = "arguments" in params
        text = f"has_arguments={has_args}"
        result = {"content": [{"type": "text", "text": text}], "isError": False}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "shutdown":
        break
""",
        encoding="utf-8",
    )
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "mcpServers": {
                "test-server": {
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
    await manager.wait_ready()
    try:
        mcp_tool_names = [
            tid for tid in registry.ids() if tid.startswith("mcp__")
        ]
        assert len(mcp_tool_names) == 1

        result = await registry.execute_tool(
            mcp_tool_names[0],
            {},
            ToolContext(workspace=str(tmp_path)),
        )
        # The fake server echoes whether 'arguments' was present in the request
        assert result.output == "has_arguments=True", (
            f"Expected 'arguments' key in tools/call params, got: {result.output}"
        )
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
    await manager.wait_ready()
    assert any(tool_id.startswith("mcp__") for tool_id in registry.ids())

    settings.delete_mcp_server("web-reader")
    await manager.restart_all()
    await manager.wait_ready()

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
    await manager.wait_ready()
    assert manager.started is True
    assert not any(tool_id.startswith("mcp__") for tool_id in registry.ids())

    from voidx.config import McpServerConfig

    settings.save_mcp_server(McpServerConfig(
        name="web-reader",
        command=sys.executable,
        args=[str(server)],
    ))
    await manager.restart_all()
    await manager.wait_ready()

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
    await manager.wait_ready()

    status = manager.statuses()[0]
    assert status.name == "missing-server"
    assert status.status == "error"
    assert "Command not found" in status.error_message


def test_mcp_tool_wrapper_id_and_description_are_instance_attributes():
    class FakeClient:
        healthy = True
        status = "connected"
        error_message = ""

        async def call_tool(self, name, arguments):
            return McpCallResult(content=[])

    wrapper = McpToolWrapper(FakeClient(), McpToolDef(name="read/url"), "my-server")

    assert isinstance(wrapper.id, str)
    assert wrapper.id.startswith("mcp__my-server__read_url_")
    assert isinstance(wrapper.description, str)
    assert "[MCP:my-server]" in wrapper.description

    # Instance attribute, not a property descriptor
    assert not isinstance(type(wrapper).id, property)
    assert not isinstance(type(wrapper).description, property)


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


class TestMcpRequestRetry:
    """Tests for _request retry behavior — _send_payload failures retried, reconnect failures not."""

    def _make_client(self, monkeypatch):
        from voidx.config import RetryConfig
        config = McpServerConfig(name="test", command="echo")
        client = McpClient(config, retry_config=RetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=False))
        client._healthy = True
        client._initialized = True
        return client

    @pytest.mark.asyncio
    async def test_send_payload_failure_retried(self, monkeypatch):
        client = self._make_client(monkeypatch)
        call_count = 0

        async def fake_send_payload(payload):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("send failed")

        async def fake_wait_for(future, timeout):
            return {"result": "ok"}

        monkeypatch.setattr(client, "_send_payload", fake_send_payload)
        monkeypatch.setattr("voidx.mcp.client.base.asyncio.wait_for", fake_wait_for)

        result = await client._request("tools/call", {"name": "test"})
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_timeout_retried(self, monkeypatch):
        import asyncio
        client = self._make_client(monkeypatch)
        call_count = 0

        async def fake_send_payload(payload):
            nonlocal call_count
            call_count += 1

        async def fake_wait_for(future, timeout):
            nonlocal call_count
            if call_count < 3:
                raise asyncio.TimeoutError()
            return {"result": "ok"}

        monkeypatch.setattr(client, "_send_payload", fake_send_payload)
        monkeypatch.setattr("voidx.mcp.client.base.asyncio.wait_for", fake_wait_for)

        result = await client._request("tools/call", {"name": "test"})
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_reconnect_failure_not_retried(self, monkeypatch):
        from voidx.mcp.client.errors import McpConnectionError
        client = self._make_client(monkeypatch)
        client._healthy = False
        client._reconnect_attempt = 99

        call_count = 0

        async def fake_send_payload(payload):
            nonlocal call_count
            call_count += 1

        monkeypatch.setattr(client, "_send_payload", fake_send_payload)

        with pytest.raises(McpConnectionError):
            await client._request("tools/call", {"name": "test"})
        assert call_count == 0

    @pytest.mark.asyncio
    async def test_protocol_error_not_retried(self, monkeypatch):
        from voidx.mcp.client.errors import McpProtocolError
        client = self._make_client(monkeypatch)
        call_count = 0

        async def fake_send_payload(payload):
            nonlocal call_count
            call_count += 1

        async def fake_wait_for(future, timeout):
            future.set_exception(McpProtocolError("bad protocol"))
            return await future

        monkeypatch.setattr(client, "_send_payload", fake_send_payload)
        monkeypatch.setattr("voidx.mcp.client.base.asyncio.wait_for", fake_wait_for)

        with pytest.raises(McpProtocolError):
            await client._request("tools/call", {"name": "test"})
        assert call_count == 1
