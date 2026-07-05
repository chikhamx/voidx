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




@pytest.mark.asyncio
async def test_v2_broadcast_turn_terminal_events():
    from voidx.ui.output.events.schema import TurnCancelled, TurnCompleted, TurnFailed

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)

    await session.broadcast_event(TurnCompleted())
    await session.broadcast_event(TurnFailed(message="boom"))
    await session.broadcast_event(TurnCancelled())

    methods = [_method(message) for message in client.messages[-3:]]
    assert methods == ["turn.completed", "turn.failed", "turn.cancelled"]
    params = [_params(message) for message in client.messages[-3:]]
    assert [param["thread_id"] for param in params] == ["t1", "t1", "t1"]
    assert params[1]["message"] == "boom"


@pytest.mark.asyncio
async def test_v2_snapshot_preserves_background_failed_thread_status():
    from voidx.ui.output.events.schema import TurnFailed

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Background")
    client = FakeClient()
    await session.connect(client)

    await session.broadcast_event(TurnFailed(thread_id="t2", message="boom"))
    await session.broadcast_snapshot()

    params = _params(client.messages[-1])
    threads = {thread["thread_id"]: thread for thread in params["threads"]}
    assert threads["t2"]["status"] == "failed"


@pytest.mark.asyncio
async def test_v2_snapshot_preserves_background_waiting_and_cancelling_statuses():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("waiting", title="Waiting")
    await session.register_thread("cancelling", title="Cancelling")
    client = FakeClient()
    await session.connect(client)

    loop = asyncio.get_running_loop()
    waiting_future = loop.create_future()
    session._run_manager.mark_running("waiting")
    session._run_manager.register_pending_request("waiting", "req-waiting", waiting_future)
    session._run_manager.mark_running("cancelling")
    await session._run_manager.cancel("cancelling")

    await session.broadcast_snapshot()

    params = _params(client.messages[-1])
    threads = {thread["thread_id"]: thread for thread in params["threads"]}
    assert threads["waiting"]["status"] == "waiting_for_user"
    assert threads["cancelling"]["status"] == "cancelling"

    session._run_manager.remove_pending_request("waiting", "req-waiting")
    waiting_future.cancel()


@pytest.mark.asyncio
async def test_v2_snapshot_refreshes_background_thread_persisted_metadata(tmp_path: Path):
    import voidx.memory.store as store
    from voidx.memory.session import MessageRow, create_session, save_message

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    saved = await create_session(
        workspace=str(tmp_path),
        title="Background",
        directory=str(tmp_path),
    )

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="active", workspace=str(tmp_path))
    await session.register_thread(saved.id, title="Stale title", directory=str(tmp_path))
    client = FakeClient()
    await session.connect(client)

    await save_message(MessageRow(session_id=saved.id, role="user", content="background complete"))
    await session.broadcast_snapshot()

    params = _params(client.messages[-1])
    threads = {thread["thread_id"]: thread for thread in params["threads"]}
    assert threads[saved.id]["message_count"] == 1
    assert threads[saved.id]["title"] == "Background"
    store._conn = None
