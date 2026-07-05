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

# ── connect / snapshot ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_connect_sends_workspace_snapshot():
    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("hello")
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()

    await session.connect(client)

    msg = _parse(client.messages[0])
    assert msg["jsonrpc"] == PROTOCOL_VERSION
    assert msg["method"] == "workspace.snapshot"
    params = msg["params"]
    assert params["active_thread_id"] == "t1"
    assert params["active_snapshot"]["thread_id"] == "t1"
    assert len(params["active_snapshot"]["nodes"]) > 0


@pytest.mark.asyncio
async def test_v2_connect_snapshot_includes_thread_info():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()

    await session.connect(client)

    params = _params(client.messages[0])
    assert len(params["threads"]) >= 1
    thread = params["threads"][0]
    assert thread["thread_id"] == "t1"


@pytest.mark.asyncio
async def test_v2_connect_snapshot_includes_runtime_status(tmp_path: Path):
    dock = BottomInputDock()
    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        workspace=str(tmp_path),
        runtime_state_provider=lambda: {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "workspace": str(tmp_path),
            "profile_configured": True,
        },
    )
    client = FakeClient()

    await session.connect(client)

    params = _params(client.messages[0])
    assert params["provider"] == "deepseek"
    assert params["model"] == "deepseek-chat"
    assert params["workspace"] == str(tmp_path)
    assert params["profile_configured"] is True


# ── event broadcasting via adapter ─────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_broadcast_event_sends_item_notification():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)

    await session.broadcast_event(AssistantStreamUpdated(text="hello"))

    # messages[0] = snapshot, messages[1] = event notification
    msg = _parse(client.messages[1])
    assert msg["jsonrpc"] == PROTOCOL_VERSION
    # AssistantStreamUpdated without a preceding Started maps to item.delta
    assert msg["method"] in ("item.started", "item.delta")
    params = msg["params"]
    assert params["thread_id"] == "t1"
    assert params["kind"] == "assistant_stream"


@pytest.mark.asyncio
async def test_v2_broadcast_event_uses_adapter_for_all_events():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)

    await session.broadcast_event(TurnStarted(text="demo"))

    msg = _parse(client.messages[1])
    assert msg["method"] == "turn.started"
    assert msg["params"]["text"] == "demo"


# ── GatewayEventConsumer ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_consumer_rebroadcasts_snapshot_on_refresh():
    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("hello")
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    consumer = GatewayEventConsumer(session)

    await consumer.handle(RefreshRequested())

    messages = [_parse(m) for m in client.messages]
    # last = snapshot, second-to-last = refresh notification
    assert messages[-1]["method"] == "workspace.snapshot"
    assert messages[-2]["method"] == "refresh.requested"


# ── broadcast_snapshot ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_broadcast_snapshot_updates_clients():
    dock = BottomInputDock()
    dock.begin_capture()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    dock.start_turn("after snapshot")

    await session.broadcast_snapshot()

    msg = _parse(client.messages[-1])
    assert msg["method"] == "workspace.snapshot"
    assert msg["params"]["active_snapshot"]["nodes"][-1]["header"].endswith("after snapshot")


# ── client failure handling ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_removes_clients_that_fail_during_broadcast():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    class FailingClient:
        def __init__(self) -> None:
            self.count = 0

        async def send_text(self, text: str) -> None:
            self.count += 1
            if self.count > 1:
                raise RuntimeError("disconnected")

    failing = FailingClient()
    healthy = FakeClient()
    await session.connect(failing)
    await session.connect(healthy)

    await session.broadcast_event(AssistantStreamUpdated(text="first"))
    await session.broadcast_event(AssistantStreamUpdated(text="second"))

    assert failing not in session.clients
    healthy_methods = [_method(m) for m in healthy.messages]
    # AssistantStreamUpdated maps to item.delta (no preceding Started)
    assert healthy_methods.count("item.delta") == 2


