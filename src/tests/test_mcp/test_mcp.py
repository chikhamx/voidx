from tests.tool_registry import build_registry
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


from voidx.config import Settings
from voidx.config import McpServerConfig
from voidx.mcp.adapters.client import McpClient
from voidx.tooling.adapters.mcp import McpGatewayTool
from voidx.mcp.application.manager import McpManager
from voidx.mcp.adapters.client import create_mcp_client
from voidx.mcp.schema import McpCallResult, McpInitializeParams, McpToolDef
from voidx.tooling.adapters.permission.in_memory_state import create_permission_service as PermissionService
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.application.registry import ToolRegistry


def test_initialize_params_serialize_to_mcp_wire_keys():
    payload = McpInitializeParams().to_dict()

    assert payload["protocolVersion"]
    assert payload["clientInfo"]["name"] == "voidx"
    assert "protocol_version" not in payload
    assert "client_info" not in payload


@pytest.mark.asyncio
async def test_mcp_manager_catalogs_tools_without_registering_direct_wrappers(tmp_path):
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
    registry = build_registry(settings=settings)
    manager = McpManager(settings.list_mcp_servers(), create_mcp_client)

    await manager.start_all()
    await manager.wait_ready()
    try:
        mcp_tool_names = [
            tool["function"]["name"]
            for tool in registry.serialize_definitions()
            if tool["function"]["name"].startswith("mcp__")
        ]
        assert mcp_tool_names == []

        entries = {entry.name: entry for entry in manager.catalog_snapshot()}
        assert [tool.name for tool in entries["web-reader"].tools] == ["read/url"]
        direct = await manager.call_tool("web-reader", "read/url", {"url": "https://example.com"})
        assert direct.content[0]["text"] == "ok"
    finally:
        await manager.stop_all()



@pytest.mark.asyncio
async def test_mcp_load_ignores_non_required_noise_fields() -> None:
    class FakeManager:
        def statuses(self):
            return [SimpleNamespace(name="typex", status="connected", tool_count=1, error_message="")]

        def catalog_snapshot(self):
            return [
                SimpleNamespace(
                    name="typex",
                    tools=[
                        McpToolDef(
                            name="typex.send_message",
                            description="Send a message",
                            inputSchema={"type": "object", "properties": {}},
                        )
                    ],
                    description="",
                )
            ]

        def server_config(self, name):
            return None

    tool = McpGatewayTool(FakeManager())

    result = await tool.execute(
        {
            "op": "load",
            "server": "typex",
            "tool": "null",
            "query": "null",
            "arguments": "ignored because load does not use arguments",
        },
        ToolContext(workspace="/tmp/workspace"),
    )

    assert result.metadata.get("error") is not True
    assert "## MCP Server: typex" in result.output
    assert "typex.send_message" in result.output


@pytest.mark.asyncio
async def test_mcp_call_requires_arguments_even_when_schema_allows_empty_object() -> None:
    class FakeManager:
        def __init__(self) -> None:
            self.called = False

        def statuses(self):
            return [SimpleNamespace(name="typex", status="connected", tool_count=1, error_message="")]

        def catalog_snapshot(self):
            return [
                SimpleNamespace(
                    name="typex",
                    tools=[McpToolDef(name="typex.ping", inputSchema={"type": "object", "properties": {}})],
                )
            ]

        def tool_def(self, server, tool):
            return McpToolDef(name="typex.ping", inputSchema={"type": "object", "properties": {}})

        async def call_tool(self, server, tool, arguments):
            self.called = True
            return McpCallResult(content=[{"type": "text", "text": "called"}])

    manager = FakeManager()
    tool = McpGatewayTool(manager)

    result = await tool.execute(
        {"op": "call", "server": "typex", "tool": "typex.ping"},
        ToolContext(workspace="/tmp/workspace"),
    )

    assert result.metadata.get("error") is True
    assert "arguments" in result.output
    assert manager.called is False
@pytest.mark.asyncio
async def test_mcp_manager_generates_missing_server_descriptions_after_cataloging():
    config = McpServerConfig(name="tavily", command="fake")
    tools = [McpToolDef(name="search", description="Search the web")]
    client = SimpleNamespace(
        list_tools=AsyncMock(return_value=tools),
        server_info={},
        instructions="",
    )
    generator = AsyncMock(return_value={"tavily": "Search the web for current information."})
    settings = SimpleNamespace(
        get_retry_config=lambda: None,
        list_mcp_servers=lambda: [config],
    )
    registry = build_registry(settings=settings)
    manager = McpManager(
        settings.list_mcp_servers(),
        create_mcp_client,
        description_generator=generator,
    )
    manager._start_servers = AsyncMock(return_value=[("tavily", client)])

    await manager._init_servers([config])
    await manager.wait_descriptions()

    generator.assert_awaited_once_with({"tavily": tools})
    assert manager.server_description("tavily") == "Search the web for current information."
    assert manager.catalog_snapshot()[0].description == "Search the web for current information."


@pytest.mark.asyncio
async def test_mcp_manager_does_not_generate_for_explicit_server_description():
    config = McpServerConfig(name="tavily", command="fake", description="Web research")
    client = SimpleNamespace(
        list_tools=AsyncMock(return_value=[McpToolDef(name="search")]),
        server_info={},
        instructions="",
    )
    generator = AsyncMock(return_value={})
    settings = SimpleNamespace(
        get_retry_config=lambda: None,
        list_mcp_servers=lambda: [config],
    )
    manager = McpManager(
        settings.list_mcp_servers(),
        create_mcp_client,
        description_generator=generator,
    )
    manager._start_servers = AsyncMock(return_value=[("tavily", client)])

    await manager._init_servers([config])
    await manager.wait_descriptions()

    generator.assert_not_awaited()
    assert manager.server_description("tavily") == "Web research"


@pytest.mark.asyncio
async def test_mcp_manager_logs_description_generation_failure():
    config = McpServerConfig(name="tavily", command="fake")
    client = SimpleNamespace(
        list_tools=AsyncMock(return_value=[McpToolDef(name="search")]),
        server_info={},
        instructions="",
    )
    generator = AsyncMock(side_effect=RuntimeError("model unavailable"))
    settings = SimpleNamespace(
        get_retry_config=lambda: None,
        list_mcp_servers=lambda: [config],
    )
    manager = McpManager(
        settings.list_mcp_servers(),
        create_mcp_client,
        description_generator=generator,
    )
    manager._start_servers = AsyncMock(return_value=[("tavily", client)])

    with patch("voidx.mcp.application.manager.log_tool_event") as log_event:
        await manager._init_servers([config])
        await manager.wait_descriptions()

    assert any(
        call.args and call.args[0] == "mcp_description_generation_failed"
        for call in log_event.call_args_list
    )


@pytest.mark.asyncio
async def test_mcp_manager_reuses_workspace_description_cache(tmp_path):
    config = McpServerConfig(name="tavily", command="fake")
    tools = [McpToolDef(name="search", description="Search the web")]
    settings = SimpleNamespace(
        get_retry_config=lambda: None,
        list_mcp_servers=lambda: [config],
    )

    async def run_manager(generator):
        client = SimpleNamespace(
            list_tools=AsyncMock(return_value=tools),
            server_info={},
            instructions="",
        )
        manager = McpManager(
        settings.list_mcp_servers(),
        create_mcp_client,
            description_generator=generator,
            workspace=str(tmp_path),
        )
        manager._start_servers = AsyncMock(return_value=[("tavily", client)])
        await manager._init_servers([config])
        await manager.wait_descriptions()
        return manager

    first_generator = AsyncMock(return_value={"tavily": "Search the web."})
    first = await run_manager(first_generator)
    second_generator = AsyncMock(return_value={"tavily": "Should not be used."})
    second = await run_manager(second_generator)

    first_generator.assert_awaited_once()
    second_generator.assert_not_awaited()
    assert first.server_description("tavily") == "Search the web."
    assert second.server_description("tavily") == "Search the web."


@pytest.mark.asyncio
async def test_call_tool_sends_empty_arguments_object(tmp_path):
    """gateway call with no args must send 'arguments': {} — not omit the field."""
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
    registry = build_registry(settings=settings)
    manager = McpManager(settings.list_mcp_servers(), create_mcp_client)
    gateway = McpGatewayTool(manager)
    registry.replace(gateway.id, gateway, gateway.description, gateway.parameters_schema())

    await manager.start_all()
    await manager.wait_ready()
    try:
        assert not any(tid.startswith("mcp__") for tid in registry.ids())

        result = await registry.execute_tool(
            "mcp",
            {"op": "call", "server": "test-server", "tool": "get_me", "arguments": {}},
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
    registry = build_registry(settings=settings)
    manager = McpManager(settings.list_mcp_servers(), create_mcp_client)

    await manager.start_all()
    await manager.wait_ready()
    assert not any(tool_id.startswith("mcp__") for tool_id in registry.ids())
    assert [entry.name for entry in manager.catalog_snapshot()] == ["web-reader"]

    settings.delete_mcp_server("web-reader")
    await manager.stop_all()
    manager = McpManager(settings.list_mcp_servers(), create_mcp_client)
    await manager.start_all()
    await manager.wait_ready()

    assert not any(tool_id.startswith("mcp__") for tool_id in registry.ids())
    assert manager.catalog_snapshot() == []


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
    registry = build_registry(settings=settings)
    manager = McpManager(settings.list_mcp_servers(), create_mcp_client)

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
    await manager.stop_all()
    manager = McpManager(settings.list_mcp_servers(), create_mcp_client)
    await manager.start_all()
    await manager.wait_ready()

    assert not any(tool_id.startswith("mcp__") for tool_id in registry.ids())
    assert [entry.name for entry in manager.catalog_snapshot()] == ["web-reader"]

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
    manager = McpManager(settings.list_mcp_servers(), create_mcp_client)

    await manager.start_all()
    await manager.wait_ready()

    status = manager.statuses()[0]
    assert status.name == "missing-server"
    assert status.status == "error"
    assert "Command not found" in status.error_message




@pytest.mark.asyncio
async def test_builtin_web_mcp_server_lists_tools():
    src_path = str(Path(__file__).parents[2])
    client = McpClient(McpServerConfig(
        name="voidx-web",
        command=sys.executable,
        args=["-m", "voidx.tooling.adapters.mcp_web_server"],
        cwd=src_path,
        env={"PYTHONPATH": os.pathsep.join(
            filter(None, [src_path, os.environ.get("PYTHONPATH", "")])
        )},
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
        monkeypatch.setattr("voidx.mcp.adapters.client.base.asyncio.wait_for", fake_wait_for)

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
        monkeypatch.setattr("voidx.mcp.adapters.client.base.asyncio.wait_for", fake_wait_for)

        result = await client._request("tools/call", {"name": "test"})
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_reconnect_failure_not_retried(self, monkeypatch):
        from voidx.mcp.domain.errors import McpConnectionError
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
        from voidx.mcp.domain.errors import McpProtocolError
        client = self._make_client(monkeypatch)
        call_count = 0

        async def fake_send_payload(payload):
            nonlocal call_count
            call_count += 1

        async def fake_wait_for(future, timeout):
            future.set_exception(McpProtocolError("bad protocol"))
            return await future

        monkeypatch.setattr(client, "_send_payload", fake_send_payload)
        monkeypatch.setattr("voidx.mcp.adapters.client.base.asyncio.wait_for", fake_wait_for)

        with pytest.raises(McpProtocolError):
            await client._request("tools/call", {"name": "test"})
        assert call_count == 1


@pytest.mark.asyncio


@pytest.mark.asyncio
async def test_mcp_start_cancellation_rolls_back_process_tasks_and_pipes(monkeypatch):
    import asyncio
    import voidx.mcp.adapters.client.base as base_module

    class FakeProcess:
        def __init__(self):
            self.pid = 4321
            self.returncode = None

    process = FakeProcess()
    handshake_started = asyncio.Event()
    reader_cancelled = asyncio.Event()
    stderr_cancelled = asyncio.Event()
    finalized = asyncio.Event()

    async def background(cancelled):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = McpClient(McpServerConfig(name="test", command="fake-server"))

    async def fake_spawn():
        client._proc = process
        client._reader = object()
        client._writer = object()
        client._read_task = asyncio.create_task(background(reader_cancelled))
        client._stderr_task = asyncio.create_task(background(stderr_cancelled))

    async def fake_handshake():
        handshake_started.set()
        await asyncio.Event().wait()

    async def finalize(owned):
        assert owned is process
        process.returncode = -15
        finalized.set()

    monkeypatch.setattr(client, "_spawn", fake_spawn)
    monkeypatch.setattr(client, "_handshake", fake_handshake)
    monkeypatch.setattr(base_module, "finalize_process_tree", finalize, raising=False)

    task = asyncio.create_task(client.start())
    await handshake_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert finalized.is_set()
    assert reader_cancelled.is_set()
    assert stderr_cancelled.is_set()
    assert client._proc is None
    assert client._reader is None
    assert client._writer is None
    assert client._read_task is None
    assert client._stderr_task is None
    assert client._pending == {}
    assert client.healthy is False


@pytest.mark.asyncio
async def test_mcp_manager_stop_all_awaits_background_start_cleanup(tmp_path, monkeypatch):
    import asyncio
    import voidx.mcp.application.manager as manager_module

    start_entered = asyncio.Event()
    stop_finished = asyncio.Event()

    class FakeClient:
        def __init__(self, config, retry_config=None):
            self.config = config

        async def start(self):
            start_entered.set()
            await asyncio.Event().wait()

        async def stop(self):
            await asyncio.sleep(0)
            stop_finished.set()

    settings = Settings(str(tmp_path))
    settings.save_mcp_server(McpServerConfig(name="slow", command="fake-server"))
    manager = McpManager(
        settings.list_mcp_servers(),
        lambda config: FakeClient(config),
    )

    await manager.start_all()
    await start_entered.wait()
    await manager.stop_all()

    assert stop_finished.is_set()
    assert manager._init_task is None
    assert manager._clients == {}
    assert manager.started is False
