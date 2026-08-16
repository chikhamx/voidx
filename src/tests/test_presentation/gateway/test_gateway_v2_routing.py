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
from voidx.presentation.adapters.persistence.transcript_snapshot import TranscriptNodeRow, replace_transcript
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events.schema import (
    AssistantStreamUpdated,
    RefreshRequested,
    TurnCompleted,
    TurnStarted,
)
from voidx.presentation.protocol import UiSubmitCommand
from voidx.presentation.protocol.v2.envelope import (
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
    PROTOCOL_VERSION,
    parse_jsonrpc_message,
)
from voidx.presentation.protocol.v2.threads import ThreadInfo


from tests.test_presentation.gateway.helpers import FakeClient, _parse, _method, _params

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
async def test_register_thread_updates_existing_metadata():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    await session.register_thread("t1", title="Updated", workspace="/workspace")

    assert session.has_thread("t1") is True
    thread = next(item for item in session.list_threads() if item.thread_id == "t1")
    assert thread.title == "Updated"
    assert thread.workspace == "/workspace"


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
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
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
    if store._conn is not None:
        store._conn.close()
    store._conn = None




@pytest.mark.asyncio
async def test_v2_snapshot_reuses_active_transcript_conversion(monkeypatch):
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    calls = 0

    from voidx.presentation.gateway.session import core as gateway_core

    original = gateway_core.tree_to_snapshot

    def counted_tree_to_snapshot(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(gateway_core, "tree_to_snapshot", counted_tree_to_snapshot)

    await session._build_workspace_snapshot(sync_persisted=False)
    await session._build_workspace_snapshot(sync_persisted=False)
    assert calls == 1

    dock.tree.new_node(
        dock.tree.root,
        node_type="message",
        header="changed",
        collapsed=False,
    )
    await session._build_workspace_snapshot(sync_persisted=False)
    assert calls == 2


@pytest.mark.asyncio
async def test_v2_switch_replaces_shared_dock_with_target_transcript(tmp_path):
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx-dock"

    await replace_transcript(
        "t2",
        [
            TranscriptNodeRow(
                session_id="t2",
                turn_id=0,
                node_id=0,
                sort_order=0,
                node_type="message",
                header="Target reply",
                body_lines=[],
                status="done",
                metadata={"payload": {"raw_text": "target reply"}},
            ),
        ],
        turn_count=1,
    )

    dock = BottomInputDock()
    dock.start_turn("Old session")
    dock.append_message("old reply")
    session = GatewaySession(lambda: dock.tree, thread_id="t1", dock=dock)
    await session.register_thread("t2", title="Second thread")

    await session.switch_thread("t2")

    headers = [node.header for node in dock.tree.root.children]
    assert any("Target reply" in header for header in headers)
    assert all("old reply" not in header for header in headers)
    if store._conn is not None:
        store._conn.close()
    store._conn = None


@pytest.mark.asyncio
async def test_v2_multi_session_switch_to_empty_thread_does_not_leak_current_tree(tmp_path):
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
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
    if store._conn is not None:
        store._conn.close()
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
async def test_v2_running_thread_events_do_not_fall_back_to_active_thread():
    dock = BottomInputDock()
    handled = []

    async def command_handler(command):
        handled.append(command)
        await session.switch_thread("t1")
        await session.broadcast_event(TurnStarted(text=command.text))
        await session.broadcast_event(AssistantStreamUpdated(text="reply", phase="text"))
        await session.broadcast_event(TurnCompleted())

    session = GatewaySession(lambda: dock.tree, thread_id="t1", command_handler=command_handler)
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Second thread")
    await session.switch_thread("t2")

    await session.handle_command(UiSubmitCommand(text="hello", thread_id="t2"))

    messages = [_parse(message) for message in client.messages]
    routed = [message for message in messages if message.get("method") in {"turn.started", "item.delta", "turn.completed"}]
    assert [message["params"]["thread_id"] for message in routed] == ["t2", "t2", "t2"]
    assert session._run_manager.status("t2") == "idle"
    assert session._run_manager.status("t1") == "idle"



@pytest.mark.asyncio
async def test_v2_ambiguous_running_thread_event_does_not_complete_active_thread():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Second thread")
    session._run_manager.mark_running("t1")
    session._run_manager.mark_running("t2")
    await session.switch_thread("t1")

    await session.broadcast_event(TurnCompleted())

    methods = [_method(message) for message in client.messages]
    assert methods.count("turn.completed") == 0
    assert session._run_manager.status("t1") == "running"
    assert session._run_manager.status("t2") == "running"

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
    import voidx.persistence.sqlite as store
    from voidx.agent.adapters.persistence.session_repository import create_session

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
    import voidx.persistence.sqlite as store
    from voidx.agent.adapters.persistence.session_repository import create_session

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
async def test_v2_async_snapshot_includes_persisted_sessions(tmp_path):
    import json
    import voidx.persistence.sqlite as store
    from voidx.agent.adapters.persistence.session_repository import create_session

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

    for _ in range(20):
        if len(client.messages) > 1:
            break
        await asyncio.sleep(0.01)

    snapshot = json.loads(client.messages[-1])
    threads = snapshot["params"]["threads"]
    assert any(t["thread_id"] == saved.id and t["title"] == "Saved thread" for t in threads)
    store._conn = None





@pytest.mark.asyncio
async def test_v2_session_submit_routes_to_explicit_thread_id():
    dock = BottomInputDock()
    captured = []

    async def handle_command(command):
        captured.append(command)

    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        command_handler=handle_command,
    )
    await session.register_thread("t2", title="Second thread")

    request = JsonRpcRequest(
        id=20,
        method="session.submit",
        params={"thread_id": "t2", "text": "hello from t2"},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result == {"ok": True}
    assert len(captured) == 1
    assert captured[0].text == "hello from t2"
    assert captured[0].thread_id == "t2"
    assert session.active_thread_id == "t1"


@pytest.mark.asyncio
async def test_v2_session_cancel_routes_to_explicit_thread_id():
    dock = BottomInputDock()
    captured = []

    async def handle_command(command):
        captured.append(command)

    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        command_handler=handle_command,
    )
    await session.register_thread("t2", title="Second thread")

    start = await session.dispatch_request(
        JsonRpcRequest(id=22, method="session.submit", params={"thread_id": "t2", "text": "start"})
    )
    assert isinstance(start, JsonRpcResult)
    captured.clear()

    request = JsonRpcRequest(
        id=21,
        method="session.cancel",
        params={"thread_id": "t2"},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result == {"ok": True}
    assert len(captured) == 1
    assert captured[0].thread_id == "t2"
    assert session.active_thread_id == "t1"


@pytest.mark.asyncio
async def test_v2_session_submit_during_running_turn_routes_as_guidance():
    dock = BottomInputDock()
    captured = []

    async def handle_command(command):
        captured.append(command)

    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        command_handler=handle_command,
    )

    first = await session.dispatch_request(
        JsonRpcRequest(id=30, method="session.submit", params={"thread_id": "t1", "text": "first"})
    )
    second = await session.dispatch_request(
        JsonRpcRequest(id=31, method="session.submit", params={"thread_id": "t1", "text": "second"})
    )

    assert isinstance(first, JsonRpcResult)
    assert isinstance(second, JsonRpcResult)
    assert second.result == {"ok": True}
    assert captured[0].kind == "submit"
    assert captured[0].text == "first"
    assert isinstance(captured[1], dict)
    assert captured[1]["kind"] == "guide"
    assert captured[1]["text"] == "second"


@pytest.mark.asyncio
async def test_v2_session_submit_enforces_global_concurrency_limit_via_run_manager():
    dock = BottomInputDock()
    captured = []

    async def handle_command(command):
        captured.append(command)

    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        command_handler=handle_command,
    )
    await session.register_thread("t2", title="Second thread")
    await session.register_thread("t3", title="Third thread")

    first = await session.dispatch_request(
        JsonRpcRequest(id=32, method="session.submit", params={"thread_id": "t1", "text": "first"})
    )
    second = await session.dispatch_request(
        JsonRpcRequest(id=33, method="session.submit", params={"thread_id": "t2", "text": "second"})
    )
    third = await session.dispatch_request(
        JsonRpcRequest(id=34, method="session.submit", params={"thread_id": "t3", "text": "third"})
    )

    assert isinstance(first, JsonRpcResult)
    assert isinstance(second, JsonRpcResult)
    assert not isinstance(third, JsonRpcResult)
    assert third.error.code == -32004
    assert [cmd.thread_id for cmd in captured] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_v2_switch_allows_running_thread():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Second thread")
    session._threads["t2"] = session._threads["t2"].model_copy(update={"status": "running"})

    result = await session.dispatch_request(
        JsonRpcRequest(id=35, method="session.switch", params={"thread_id": "t2"})
    )

    assert isinstance(result, JsonRpcResult)
    assert result.result["active_thread_id"] == "t2"
    assert session.active_thread_id == "t2"


@pytest.mark.asyncio
async def test_v2_session_response_routes_by_thread_id_with_duplicate_request_ids():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Second thread")

    loop = asyncio.get_running_loop()
    t1_future = loop.create_future()
    t2_future = loop.create_future()
    session._run_manager.register_pending_request("t1", "duplicate", t1_future)
    session._run_manager.register_pending_request("t2", "duplicate", t2_future)

    result = await session.dispatch_request(
        JsonRpcRequest(
            id=36,
            method="session.respond",
            params={"thread_id": "t2", "request_id": "duplicate", "value": "target"},
        )
    )

    assert isinstance(result, JsonRpcResult)
    assert not t1_future.done()
    assert t2_future.done()
    assert t2_future.result().value == "target"


@pytest.mark.asyncio
async def test_v2_session_response_without_thread_id_falls_back_to_unique_request_id():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Second thread")

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    session._run_manager.register_pending_request("t2", "permission-1", future)

    result = await session.dispatch_request(
        JsonRpcRequest(
            id=37,
            method="session.respond",
            params={"request_id": "permission-1", "value": "allow"},
        )
    )

    assert isinstance(result, JsonRpcResult)
    assert future.done()
    assert future.result().value == "allow"


@pytest.mark.asyncio
async def test_v2_guidance_submit_returns_failure_when_handler_rejects():
    dock = BottomInputDock()
    guide_results: list[bool] = [False]

    async def handle_command(command):
        if isinstance(command, dict) and command.get("kind") == "guide":
            return guide_results.pop(0)
        return None

    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        command_handler=handle_command,
    )

    first = await session.dispatch_request(
        JsonRpcRequest(id=40, method="session.submit", params={"thread_id": "t1", "text": "first"})
    )
    second = await session.dispatch_request(
        JsonRpcRequest(id=41, method="session.submit", params={"thread_id": "t1", "text": "keep going"})
    )

    assert isinstance(first, JsonRpcResult)
    assert first.result == {"ok": True}
    assert isinstance(second, JsonRpcResult)
    assert second.result == {"ok": False}


async def _replace_thread_with_turns(session_id: str, turn_count: int) -> None:
    rows = [
        TranscriptNodeRow(
            session_id=session_id,
            turn_id=turn_id,
            node_id=0,
            sort_order=0,
            node_type="turn",
            header=f"turn {turn_id}",
            status="done",
        )
        for turn_id in range(turn_count)
    ]
    await replace_transcript(session_id, rows, turn_count=turn_count)


@pytest.mark.asyncio
async def test_v2_switch_with_turn_limit_broadcasts_windowed_snapshot(tmp_path):
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx-window"

    await _replace_thread_with_turns("t2", 4)
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Second thread")

    await session._method_session_switch({"thread_id": "t2", "turn_limit": 2})

    message = _parse(client.messages[-1])
    snapshot = message["params"]["active_snapshot"]
    assert snapshot["thread_id"] == "t2"
    assert snapshot["windowed"] is True
    assert [node["header"] for node in snapshot["nodes"]] == ["turn 2", "turn 3"]
    assert snapshot["before_turn_id"] == 2
    assert snapshot["after_turn_id"] == 3
    assert snapshot["has_earlier"] is True
    assert snapshot["has_later"] is False

    if store._conn is not None:
        store._conn.close()
    store._conn = None


@pytest.mark.asyncio
async def test_v2_switch_without_turn_limit_keeps_full_snapshot_fallback(tmp_path):
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx-full"

    await _replace_thread_with_turns("t2", 3)
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Second thread")

    await session._method_session_switch({"thread_id": "t2"})

    snapshot = _parse(client.messages[-1])["params"]["active_snapshot"]
    assert snapshot["windowed"] is False
    assert [node["header"] for node in snapshot["nodes"]] == ["turn 0", "turn 1", "turn 2"]

    if store._conn is not None:
        store._conn.close()
    store._conn = None


@pytest.mark.asyncio
async def test_v2_transcript_page_does_not_change_active_thread(tmp_path):
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx-page"

    await _replace_thread_with_turns("t2", 4)
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    await session.register_thread("t2", title="Second thread")

    result = await session._method_transcript_page({
        "thread_id": "t2",
        "before_turn_id": 2,
        "turn_limit": 1,
    })

    assert session.active_thread_id == "t1"
    assert result["thread_id"] == "t2"
    assert result["windowed"] is True
    assert [node["header"] for node in result["nodes"]] == ["turn 1"]
    assert result["before_turn_id"] == 1
    assert result["after_turn_id"] == 1
    assert result["has_earlier"] is True
    assert result["has_later"] is True

    if store._conn is not None:
        store._conn.close()
    store._conn = None


@pytest.mark.asyncio
async def test_v2_transcript_page_is_registered_with_dispatch(tmp_path):
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx-page-dispatch"

    await _replace_thread_with_turns("t2", 2)
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    await session.register_thread("t2", title="Second thread")

    result = await session.dispatch_request(JsonRpcRequest(
        id=42,
        method="transcript.page",
        params={"thread_id": "t2", "turn_limit": 1},
    ))

    assert isinstance(result, JsonRpcResult)
    assert result.result["thread_id"] == "t2"
    assert result.result["windowed"] is True

    if store._conn is not None:
        store._conn.close()
    store._conn = None


@pytest.mark.asyncio
async def test_v2_window_preference_survives_terminal_snapshot(tmp_path):
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx-terminal-window"

    await _replace_thread_with_turns("t2", 4)
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Second thread")
    client.messages.clear()

    result = await session.dispatch_request(JsonRpcRequest(
        id=43,
        method="session.switch",
        params={"thread_id": "t2", "turn_limit": 2},
    ), client=client)

    assert isinstance(result, JsonRpcResult)
    client.messages.clear()
    await session.broadcast_event(TurnCompleted())

    snapshot = _parse(client.messages[-1])["params"]["active_snapshot"]
    assert snapshot["windowed"] is True
    assert [node["header"] for node in snapshot["nodes"]] == ["turn 2", "turn 3"]

    if store._conn is not None:
        store._conn.close()
    store._conn = None


@pytest.mark.asyncio
async def test_v2_window_preference_is_isolated_per_client(tmp_path):
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx-client-window"

    await _replace_thread_with_turns("t2", 4)
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    window_client = FakeClient()
    legacy_client = FakeClient()
    await session.connect(window_client)
    await session.connect(legacy_client)
    await session.register_thread("t2", title="Second thread")
    window_client.messages.clear()
    legacy_client.messages.clear()

    result = await session.dispatch_request(JsonRpcRequest(
        id=44,
        method="session.switch",
        params={"thread_id": "t2", "turn_limit": 2},
    ), client=window_client)

    assert isinstance(result, JsonRpcResult)
    window_client.messages.clear()
    legacy_client.messages.clear()
    await session.broadcast_event(TurnCompleted())

    window_snapshot = _parse(window_client.messages[-1])["params"]["active_snapshot"]
    legacy_snapshot = _parse(legacy_client.messages[-1])["params"]["active_snapshot"]
    assert window_snapshot["windowed"] is True
    assert legacy_snapshot["windowed"] is False

    if store._conn is not None:
        store._conn.close()
    store._conn = None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["session.switch", "transcript.page"])
@pytest.mark.parametrize("thread_id", ["", [], {}, True])
async def test_v2_session_methods_reject_invalid_thread_id(method, thread_id):
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    params = {"thread_id": thread_id}
    if method == "transcript.page":
        params["turn_limit"] = 1
    result = await session.dispatch_request(JsonRpcRequest(
        id=45,
        method=method,
        params=params,
    ))

    assert isinstance(result, JsonRpcError)
    assert result.error.code == -32602


@pytest.mark.asyncio
async def test_v2_transcript_page_records_window_preference_for_terminal_snapshot(tmp_path):
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx-page-window"

    await _replace_thread_with_turns("t2", 4)
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t2")
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Second thread")
    client.messages.clear()

    result = await session.dispatch_request(JsonRpcRequest(
        id=46,
        method="transcript.page",
        params={"thread_id": "t2", "turn_limit": 2},
    ), client=client)

    assert isinstance(result, JsonRpcResult)
    await session.broadcast_event(TurnCompleted())

    snapshot = _parse(client.messages[-1])["params"]["active_snapshot"]
    assert snapshot["windowed"] is True
    assert [node["header"] for node in snapshot["nodes"]] == ["turn 2", "turn 3"]

    if store._conn is not None:
        store._conn.close()
    store._conn = None


@pytest.mark.asyncio
async def test_v2_session_switch_without_turn_limit_resets_client_window_preference(tmp_path):
    import voidx.persistence.sqlite as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx-switch-full"

    await _replace_thread_with_turns("t2", 4)
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    await session.register_thread("t2", title="Second thread")

    result = await session.dispatch_request(JsonRpcRequest(
        id=47,
        method="session.switch",
        params={"thread_id": "t2", "turn_limit": 2},
    ), client=client)
    assert isinstance(result, JsonRpcResult)
    result = await session.dispatch_request(JsonRpcRequest(
        id=48,
        method="session.switch",
        params={"thread_id": "t2"},
    ), client=client)
    assert isinstance(result, JsonRpcResult)

    client.messages.clear()
    await session.broadcast_event(TurnCompleted())
    snapshot = _parse(client.messages[-1])["params"]["active_snapshot"]
    assert snapshot["windowed"] is False

    if store._conn is not None:
        store._conn.close()
    store._conn = None
