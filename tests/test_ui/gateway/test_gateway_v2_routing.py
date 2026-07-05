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

# ── multi-session routing ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_multi_session_registers_thread():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    await session.register_thread("t2", title="Second thread")

    threads = session.list_threads()
    thread_ids = [t.thread_id for t in threads]
    assert "t1" in thread_ids
    assert "t2" in thread_ids


@pytest.mark.asyncio
async def test_v2_multi_session_switch_active_thread():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Second thread")

    assert session.active_thread_id == "t1"

    await session.switch_thread("t2")
    assert session.active_thread_id == "t2"


@pytest.mark.asyncio
async def test_v2_multi_session_switch_broadcasts_new_snapshot():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Second thread")

    await session.switch_thread("t2")

    # Last message should be a workspace.snapshot with t2 as active
    msg = _parse(client.messages[-1])
    assert msg["method"] == "workspace.snapshot"
    assert msg["params"]["active_thread_id"] == "t2"


@pytest.mark.asyncio
async def test_v2_multi_session_switch_uses_target_thread_transcript(tmp_path):
    import voidx.memory.store as store

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"

    await replace_transcript(
        "t2",
        [
            TranscriptNodeRow(
                session_id="t2",
                turn_id=0,
                node_id=0,
                sort_order=0,
                node_type="message",
                header="Target thread content",
                body_lines=["from persisted transcript"],
                status="done",
                metadata={"payload": {"raw_text": "from t2 transcript"}},
            ),
        ],
        turn_count=1,
    )

    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("current tree should not leak")
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Second thread")

    await session.switch_thread("t2")

    msg = _parse(client.messages[-1])
    assert msg["method"] == "workspace.snapshot"
    params = msg["params"]
    assert params["active_thread_id"] == "t2"
    assert params["active_snapshot"]["thread_id"] == "t2"
    assert params["active_snapshot"]["nodes"][0]["header"] == "Target thread content"
    assert params["active_snapshot"]["nodes"][0]["payload"]["raw_text"] == "from t2 transcript"
    assert all("current tree should not leak" not in node["header"] for node in params["active_snapshot"]["nodes"])
    store._conn = None


@pytest.mark.asyncio
async def test_v2_multi_session_switch_to_empty_thread_does_not_leak_current_tree(tmp_path):
    import voidx.memory.store as store

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx-empty"

    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("current tree should not leak")
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Empty thread")

    await session.switch_thread("t2")

    msg = _parse(client.messages[-1])
    params = msg["params"]
    assert params["active_thread_id"] == "t2"
    assert params["active_snapshot"]["thread_id"] == "t2"
    assert params["active_snapshot"]["nodes"] == []
    store._conn = None


@pytest.mark.asyncio
async def test_v2_multi_session_event_routes_to_correct_adapter():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Second thread")

    # Broadcast an event for t2 — should use t2's adapter
    await session.broadcast_event(TurnStarted(text="t2 message"), thread_id="t2")

    msg = _parse(client.messages[-1])
    assert msg["method"] == "turn.started"
    assert msg["params"]["thread_id"] == "t2"


@pytest.mark.asyncio
async def test_v2_multi_session_unregister_thread():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Second thread")

    await session.unregister_thread("t2")

    threads = session.list_threads()
    assert "t2" not in [t.thread_id for t in threads]


@pytest.mark.asyncio
async def test_v2_session_switch_method_via_jsonrpc():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Second thread")

    request = JsonRpcRequest(
        id=3, method="session.switch", params={"thread_id": "t2"},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result["active_thread_id"] == "t2"
    assert session.active_thread_id == "t2"


@pytest.mark.asyncio
async def test_v2_session_list_method_via_jsonrpc():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Second thread")

    request = JsonRpcRequest(id=4, method="session.list", params={})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    thread_ids = [t["thread_id"] for t in result.result["threads"]]
    assert "t1" in thread_ids
    assert "t2" in thread_ids


@pytest.mark.asyncio
async def test_v2_session_list_includes_persisted_sessions(tmp_path):
    import voidx.memory.store as store
    from voidx.memory.session import create_session

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    saved = await create_session(
        workspace="/Users/chikham/workspace/voidx",
        title="Saved thread",
        directory="/Users/chikham/workspace/voidx",
    )

    dock = BottomInputDock()
    session = GatewaySession(
        lambda: dock.tree,
        workspace="/Users/chikham/workspace/voidx",
    )

    request = JsonRpcRequest(id=14, method="session.list", params={})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    threads = result.result["threads"]
    assert any(t["thread_id"] == saved.id and t["title"] == "Saved thread" for t in threads)
    assert saved.id in [t.thread_id for t in session.list_threads()]
    store._conn = None


@pytest.mark.asyncio
async def test_v2_session_list_includes_persisted_sessions_from_other_workspaces(tmp_path):
    import voidx.memory.store as store
    from voidx.memory.session import create_session

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    current = await create_session(
        workspace="/Users/chikham/workspace/voidx",
        title="voidx thread",
        directory="/Users/chikham/workspace/voidx",
    )
    other = await create_session(
        workspace="/Users/chikham/workspace/imcore-sdk",
        title="imcore thread",
        directory="/Users/chikham/workspace/imcore-sdk",
    )

    dock = BottomInputDock()
    session = GatewaySession(
        lambda: dock.tree,
        workspace="/Users/chikham/workspace/voidx",
    )

    request = JsonRpcRequest(id=15, method="session.list", params={})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    threads_by_id = {t["thread_id"]: t for t in result.result["threads"]}
    assert threads_by_id[current.id]["workspace"] == "/Users/chikham/workspace/voidx"
    assert threads_by_id[other.id]["workspace"] == "/Users/chikham/workspace/imcore-sdk"
    store._conn = None


@pytest.mark.asyncio
async def test_v2_initial_snapshot_includes_persisted_sessions(tmp_path):
    import json
    import voidx.memory.store as store
    from voidx.memory.session import create_session

    class Client:
        def __init__(self):
            self.messages: list[str] = []

        async def send_text(self, text: str) -> None:
            self.messages.append(text)

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    saved = await create_session(
        workspace="/Users/chikham/workspace/voidx",
        title="Saved thread",
        directory="/Users/chikham/workspace/voidx",
    )

    dock = BottomInputDock()
    session = GatewaySession(
        lambda: dock.tree,
        workspace="/Users/chikham/workspace/voidx",
    )
    client = Client()

    await session.connect(client)

    snapshot = json.loads(client.messages[0])
    threads = snapshot["params"]["threads"]
    assert any(t["thread_id"] == saved.id and t["title"] == "Saved thread" for t in threads)
    store._conn = None



