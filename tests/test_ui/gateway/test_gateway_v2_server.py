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

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.ui.gateway.adapter import UiEventItemAdapter
from voidx.ui.gateway.session import GatewayEventConsumer, GatewaySession
from voidx.memory.transcript import TranscriptNodeRow, replace_transcript
from voidx.ui.output.dock import BottomInputDock
from voidx.ui.output.events.schema import (
    AssistantStreamUpdated,
    RefreshRequested,
    TurnStarted,
)
from voidx.ui.protocol.v2.envelope import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
    PROTOCOL_VERSION,
    parse_jsonrpc_message,
)
from voidx.ui.protocol.v2.threads import ThreadInfo


from tests.test_ui.gateway.helpers import FakeClient, _parse, _method, _params

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
    from voidx.ui.gateway.server import parse_jsonrpc_message_str

    raw = '{"jsonrpc":"2.0","id":1,"method":"session.submit","params":{"text":"hi"}}'
    msg = parse_jsonrpc_message_str(raw)
    assert msg.method == "session.submit"
    assert msg.params == {"text": "hi"}
    assert msg.id == 1


def test_parse_jsonrpc_notification_from_string():
    from voidx.ui.gateway.server import parse_jsonrpc_message_str

    raw = '{"jsonrpc":"2.0","method":"item.started","params":{"kind":"message"}}'
    msg = parse_jsonrpc_message_str(raw)
    assert msg.method == "item.started"
    assert not hasattr(msg, "id") or getattr(msg, "id", None) is None


def test_parse_jsonrpc_message_str_raises_on_invalid_json():
    from voidx.ui.gateway.server import parse_jsonrpc_message_str
    from voidx.ui.protocol.v2.envelope import ParseError

    with pytest.raises(ParseError):
        parse_jsonrpc_message_str("not json at all")


# ── server: WebSocket integration ──────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_websocket_connect_sends_workspace_snapshot():
    import websockets
    from voidx.ui.gateway.server import GatewayServer

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
    from voidx.ui.gateway.server import GatewayServer

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
    from voidx.ui.gateway.server import GatewayServer

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
    from voidx.ui.gateway.server import GatewayServer

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


