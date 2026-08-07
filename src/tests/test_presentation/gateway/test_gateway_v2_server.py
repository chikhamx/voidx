"""Tests for v2 JSON-RPC gateway session and server.

The v2 gateway replaces v1 envelope broadcasting with:
- WorkspaceSnapshot on connect (v2 model)
- UiEventItemAdapter for event → Item notification conversion
- MethodDispatch for JSON-RPC request handling
- JSON-RPC notification broadcasting (not v1 UiEventEnvelope)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


from voidx.presentation.gateway.adapter import UiEventItemAdapter
from voidx.presentation.gateway.session import GatewayEventConsumer, GatewaySession
from voidx.presentation.transcript_snapshot import TranscriptNodeRow, replace_transcript
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events.schema import (
    AssistantStreamUpdated,
    RefreshRequested,
    TurnStarted,
)
from voidx.presentation.protocol.v2.envelope import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
    PROTOCOL_VERSION,
    parse_jsonrpc_message,
)
from voidx.presentation.protocol.v2.threads import ThreadInfo


from tests.test_presentation.gateway.helpers import FakeClient, _parse, _method, _params

# ── v1 compatibility: request/handle_response still works ──────────────
# (GatewaySession.request is registered as TUI external_request_handler
#  in run_loop.py, so the v1 signature must be preserved even though
#  the wire format is now v2 JSON-RPC)


@pytest.mark.asyncio
async def test_v2_session_has_request_method_for_tui_compatibility():
    """GatewaySession must still expose .request() for run_loop.py registration."""
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    assert hasattr(session, "request")
    assert hasattr(session, "set_command_handler")
    assert hasattr(session, "handle_command")


# ── server: JSON-RPC message parsing ───────────────────────────────────


def test_parse_jsonrpc_message_from_string():
    from voidx.presentation.gateway.server import parse_jsonrpc_message_str

    raw = '{"jsonrpc":"2.0","id":1,"method":"session.submit","params":{"text":"hi"}}'
    msg = parse_jsonrpc_message_str(raw)
    assert msg.method == "session.submit"
    assert msg.params == {"text": "hi"}
    assert msg.id == 1


def test_parse_jsonrpc_notification_from_string():
    from voidx.presentation.gateway.server import parse_jsonrpc_message_str

    raw = '{"jsonrpc":"2.0","method":"item.started","params":{"kind":"message"}}'
    msg = parse_jsonrpc_message_str(raw)
    assert msg.method == "item.started"
    assert not hasattr(msg, "id") or getattr(msg, "id", None) is None


def test_parse_jsonrpc_message_str_raises_on_invalid_json():
    from voidx.presentation.gateway.server import parse_jsonrpc_message_str
    from voidx.presentation.protocol.v2.envelope import ParseError

    with pytest.raises(ParseError):
        parse_jsonrpc_message_str("not json at all")


# ── server: WebSocket integration ──────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_websocket_connect_sends_workspace_snapshot():
    import websockets
    from voidx.presentation.gateway.server import GatewayServer

    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("hello")
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    server = GatewayServer(session, host="127.0.0.1", port=0, token="secret")
    await server.start()
    try:
        async with websockets.connect(server.url) as websocket:
            raw = await websocket.recv()
            msg = json.loads(raw)
            assert msg["jsonrpc"] == PROTOCOL_VERSION
            assert msg["method"] == "workspace.snapshot"
            assert msg["params"]["active_thread_id"] == "t1"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_v2_websocket_broadcasts_item_notification():
    import websockets
    from voidx.presentation.gateway.server import GatewayServer

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    server = GatewayServer(session, host="127.0.0.1", port=0, token="secret")
    await server.start()
    try:
        async with websockets.connect(server.url) as websocket:
            await websocket.recv()  # snapshot
            await session.broadcast_event(TurnStarted(text="hi web"))
            raw = await websocket.recv()
            msg = json.loads(raw)
            assert msg["method"] == "turn.started"
            assert msg["params"]["text"] == "hi web"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_v2_websocket_dispatches_jsonrpc_request():
    import websockets
    from voidx.presentation.gateway.server import GatewayServer

    dock = BottomInputDock()
    received: list[str] = []

    async def handle_submit(params):
        received.append(params["text"])
        return {"ok": True}

    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    session.methods.register("session.submit", handle_submit)
    server = GatewayServer(session, host="127.0.0.1", port=0, token="secret")
    await server.start()
    try:
        async with websockets.connect(server.url) as websocket:
            await websocket.recv()  # snapshot
            request = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.submit",
                "params": {"text": "from web"},
            })
            await websocket.send(request)
            raw = await websocket.recv()
            result = json.loads(raw)
            assert result["jsonrpc"] == PROTOCOL_VERSION
            assert result["id"] == 1
            assert result["result"] == {"ok": True}
            assert received == ["from web"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_v2_websocket_returns_error_for_unknown_method():
    import websockets
    from voidx.presentation.gateway.server import GatewayServer

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    server = GatewayServer(session, host="127.0.0.1", port=0, token="secret")
    await server.start()
    try:
        async with websockets.connect(server.url) as websocket:
            await websocket.recv()  # snapshot
            request = json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "nonexistent.method",
                "params": {},
            })
            await websocket.send(request)
            raw = await websocket.recv()
            error = json.loads(raw)
            assert error["jsonrpc"] == PROTOCOL_VERSION
            assert error["id"] == 2
            assert "error" in error
            assert error["error"]["code"] == -32601
    finally:
        await server.stop()




@pytest.mark.asyncio
async def test_v2_jsonrpc_result_routes_ui_response_by_thread_id_payload():
    from voidx.presentation.gateway.server import GatewayServer
    from voidx.presentation.protocol.requests import UiChoiceRequest, UiResponse

    class WebSocketStub:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Second thread")
    server = GatewayServer(session)
    client = FakeClient()
    await session.connect(client)
    await session.switch_thread("t2")

    pending = asyncio.create_task(
        session.request(
            UiChoiceRequest(
                request_id="choice_t2",
                prompt="Pick one",
                choices=[("Ok", "ok", "Ok option")],
            ),
        ),
    )
    await asyncio.sleep(0)
    await session.switch_thread("t1")

    await server._handle_message(
        WebSocketStub(),
        '{"jsonrpc":"2.0","id":"choice_t2","result":{"thread_id":"t2","value":"ok"}}',
    )

    assert await asyncio.wait_for(pending, timeout=1) == UiResponse(
        request_id="choice_t2",
        value="ok",
    )


@pytest.mark.asyncio
async def test_websocket_client_send_text_does_not_wait_for_blocked_send():
    from voidx.presentation.gateway.server import _WebSocketClient

    class BlockingWebSocket:
        def __init__(self) -> None:
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()
            self.closed = False

        async def send(self, text: str) -> None:
            self.send_started.set()
            await self.release_send.wait()

        async def close(self) -> None:
            self.closed = True
            self.release_send.set()

    websocket = BlockingWebSocket()
    client = _WebSocketClient(websocket, queue_maxsize=4)
    await client.start()
    try:
        await client.send_text("first")
        await asyncio.wait_for(websocket.send_started.wait(), timeout=0.2)
        await asyncio.wait_for(client.send_text("second"), timeout=0.05)
    finally:
        await client.close()
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_websocket_client_priority_message_survives_full_queue():
    from voidx.presentation.gateway.server import _WebSocketClient

    class PausedWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.release_send = asyncio.Event()

        async def send(self, text: str) -> None:
            self.sent.append(text)
            await self.release_send.wait()

        async def close(self) -> None:
            self.release_send.set()

    websocket = PausedWebSocket()
    client = _WebSocketClient(websocket, queue_maxsize=2)
    await client.start()
    try:
        await client.send_text("blocking-low")
        await asyncio.sleep(0)
        await client.send_text("queued-low")
        await client.send_text("priority", priority=True)
        queued = list(getattr(client, "_send_queue")._queue)
        assert "priority" in queued
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_handle_message_uses_client_queue_for_jsonrpc_result():
    from voidx.presentation.gateway.server import GatewayServer

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    session.methods.register("ping", lambda params: {"pong": True})
    server = GatewayServer(session, host="127.0.0.1", port=0, token="")

    class ClientStub:
        def __init__(self) -> None:
            self.sent: list[tuple[str, bool]] = []

        async def send_text(self, text: str, *, priority: bool = False) -> None:
            self.sent.append((text, priority))

    client = ClientStub()
    await server._handle_message(client, json.dumps({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "ping",
        "params": {},
    }))

    assert len(client.sent) == 1
    raw, priority = client.sent[0]
    assert priority is True
    msg = json.loads(raw)
    assert msg["id"] == 7
    assert msg["result"] == {"pong": True}


@pytest.mark.asyncio
async def test_websocket_client_send_loop_times_out_blocked_send():
    from voidx.presentation.gateway.server import _WebSocketClient

    class NeverSendingWebSocket:
        def __init__(self) -> None:
            self.send_started = asyncio.Event()
            self.closed = False

        async def send(self, text: str) -> None:
            self.send_started.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

    websocket = NeverSendingWebSocket()
    client = _WebSocketClient(websocket, queue_maxsize=4, send_timeout=0.01)
    await client.start()

    await client.send_text("will-timeout")
    await asyncio.wait_for(websocket.send_started.wait(), timeout=0.2)
    await asyncio.sleep(0.05)

    assert getattr(client, "_closed") is True
    await client.close()


@pytest.mark.asyncio
async def test_websocket_client_handles_queue_full_without_crash():
    from voidx.presentation.gateway.server import _WebSocketClient

    class PausedWebSocket:
        def __init__(self) -> None:
            self.release_send = asyncio.Event()

        async def send(self, text: str) -> None:
            await self.release_send.wait()

        async def close(self) -> None:
            self.release_send.set()

    websocket = PausedWebSocket()
    client = _WebSocketClient(websocket, queue_maxsize=1)
    await client.start()
    try:
        await client.send_text("blocking")
        await asyncio.sleep(0)
        await client.send_text("queued")
        await client.send_text("dropped")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_websocket_client_writes_tool_log_for_queue_full(monkeypatch):
    from voidx.presentation.gateway import server as server_module

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        server_module,
        "log_tool_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    class PausedWebSocket:
        def __init__(self) -> None:
            self.release_send = asyncio.Event()

        async def send(self, text: str) -> None:
            await self.release_send.wait()

        async def close(self) -> None:
            self.release_send.set()

    websocket = PausedWebSocket()
    client = server_module._WebSocketClient(websocket, queue_maxsize=1)
    await client.start()
    try:
        await client.send_text("blocking")
        await asyncio.sleep(0)
        await client.send_text("queued")
        await client.send_text("dropped")
    finally:
        await client.close()

    assert events
    assert events[0][0] == "gateway_send_queue_full"
    assert events[0][1]["tool_name"] == "gateway"
    assert "dropping message" in events[0][1]["message"]


@pytest.mark.asyncio
async def test_websocket_client_writes_tool_log_for_send_timeout(monkeypatch):
    from voidx.presentation.gateway import server as server_module

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        server_module,
        "log_tool_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    class NeverSendingWebSocket:
        def __init__(self) -> None:
            self.send_started = asyncio.Event()

        async def send(self, text: str) -> None:
            self.send_started.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            pass

    websocket = NeverSendingWebSocket()
    client = server_module._WebSocketClient(websocket, queue_maxsize=4, send_timeout=0.01)
    await client.start()
    await client.send_text("will-timeout")
    await asyncio.wait_for(websocket.send_started.wait(), timeout=0.2)
    await asyncio.sleep(0.05)
    await client.close()

    assert events
    assert events[0][0] == "gateway_websocket_send_timeout"
    assert events[0][1]["tool_name"] == "gateway"
    assert "timed out" in events[0][1]["message"]



@pytest.mark.asyncio
async def test_websocket_client_priority_send_drops_snapshot_over_response():
    from voidx.presentation.gateway.server import _WebSocketClient

    class PausedWebSocket:
        def __init__(self) -> None:
            self.release_send = asyncio.Event()

        async def send(self, text: str) -> None:
            await self.release_send.wait()

        async def close(self) -> None:
            self.release_send.set()

    websocket = PausedWebSocket()
    client = _WebSocketClient(websocket, queue_maxsize=2)
    await client.start()
    try:
        snapshot = json.dumps({"jsonrpc": "2.0", "method": "workspace.snapshot", "params": {}})
        response = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": "ok"}})
        await client.send_text(snapshot)
        await client.send_text(response)
        await asyncio.sleep(0)
        await client.send_text(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"value": "priority"}}), priority=True)
        queued = list(client._send_queue._queue)
        assert json.loads(queued[0])["id"] == 1
        assert json.loads(queued[1])["id"] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_websocket_client_priority_send_falls_back_to_head_when_no_droppable():
    from voidx.presentation.gateway.server import _WebSocketClient

    class PausedWebSocket:
        def __init__(self) -> None:
            self.release_send = asyncio.Event()

        async def send(self, text: str) -> None:
            await self.release_send.wait()

        async def close(self) -> None:
            self.release_send.set()

    websocket = PausedWebSocket()
    client = _WebSocketClient(websocket, queue_maxsize=2)
    await client.start()
    try:
        resp1 = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": "a"}})
        resp2 = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"value": "b"}})
        await client.send_text(resp1)
        await client.send_text(resp2)
        await asyncio.sleep(0)
        await client.send_text(json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"value": "c"}}), priority=True)
        queued = list(client._send_queue._queue)
        assert len(queued) == 2
        assert json.loads(queued[0])["id"] == 2
        assert json.loads(queued[1])["id"] == 3
    finally:
        await client.close()
