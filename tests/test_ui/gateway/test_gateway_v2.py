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


# ── helpers ────────────────────────────────────────────────────────────


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, text: str) -> None:
        self.messages.append(text)


def _parse(msg: str) -> dict:
    return json.loads(msg)


def _method(msg: str) -> str:
    return _parse(msg)["method"]


def _params(msg: str) -> dict:
    return _parse(msg)["params"]


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


# ── JSON-RPC method dispatch ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_dispatches_session_submit_method():
    dock = BottomInputDock()
    received: list[str] = []

    async def handle_submit(params):
        received.append(params["text"])
        return {"ok": True}

    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    session.methods.register("session.submit", handle_submit)
    client = FakeClient()
    await session.connect(client)

    request = JsonRpcRequest(id=1, method="session.submit", params={"text": "hello web"})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.id == 1
    assert result.result == {"ok": True}
    assert received == ["hello web"]


@pytest.mark.asyncio
async def test_v2_dispatch_returns_method_not_found_for_unknown_method():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    request = JsonRpcRequest(id=2, method="nonexistent.method", params={})
    result = await session.dispatch_request(request)

    # dispatch_request returns JsonRpcResult | JsonRpcError
    assert result.id == 2
    # It should be an error (method not found)
    assert hasattr(result, "error")
    assert result.error.code == -32601


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



# ── session CRUD methods ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_session_create_method_registers_thread(tmp_path):
    """session.create persists a session and registers a thread in-memory."""
    import voidx.memory.store as store

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    request = JsonRpcRequest(
        id=10, method="session.create", params={"title": "New thread"},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    info = result.result
    assert info["title"] == "New thread"
    assert info["status"] == "idle"
    assert info["thread_id"] in [t.thread_id for t in session.list_threads()]
    store._conn = None


@pytest.mark.asyncio
async def test_v2_session_rename_method_updates_title(tmp_path):
    import voidx.memory.store as store

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    from voidx.memory.session import create_session

    info = await create_session()

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id=info.id)

    request = JsonRpcRequest(
        id=11, method="session.rename",
        params={"thread_id": info.id, "title": "Renamed"},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result == {"ok": True}
    thread = next(t for t in session.list_threads() if t.thread_id == info.id)
    assert thread.title == "Renamed"
    store._conn = None


@pytest.mark.asyncio
async def test_v2_session_delete_method_removes_thread(tmp_path):
    import voidx.memory.store as store

    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    from voidx.memory.session import create_session

    info = await create_session()

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id=info.id)

    request = JsonRpcRequest(
        id=12, method="session.delete", params={"thread_id": info.id},
    )
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result == {"ok": True}
    assert info.id not in [t.thread_id for t in session.list_threads()]
    store._conn = None


@pytest.mark.asyncio
async def test_v2_session_switch_rejects_running_thread():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2", title="Second")

    # Mark t2 as running
    session._threads["t2"] = session._threads["t2"].model_copy(
        update={"status": "running"},
    )

    request = JsonRpcRequest(
        id=13, method="session.switch", params={"thread_id": "t2"},
    )
    result = await session.dispatch_request(request)

    assert hasattr(result, "error")
    assert result.error.code == -32001  # ERR_TURN_IN_PROGRESS


# ── diff.review.apply writes files ────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_diff_apply_writes_approved_hunks_to_file(tmp_path):
    """diff.apply should rebuild files with only approved hunks applied."""
    target = tmp_path / "sample.txt"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    # Diff: replace line2 with line2-modified
    diff_text = (
        f"--- {target}\n"
        f"+++ {target}\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+line2-modified\n"
        " line3\n"
    )

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    review_req = JsonRpcRequest(
        id=20, method="diff.review", params={"diff": diff_text},
    )
    review_result = await session.dispatch_request(review_req)
    assert isinstance(review_result, JsonRpcResult)
    review_id = review_result.result["review_id"]

    # Approve the single hunk
    decide_req = JsonRpcRequest(
        id=21, method="diff.decide",
        params={"review_id": review_id, "file_path": str(target),
                "hunk_index": 0, "decision": "approved"},
    )
    await session.dispatch_request(decide_req)

    # Apply
    apply_req = JsonRpcRequest(
        id=22, method="diff.apply", params={"review_id": review_id},
    )
    apply_result = await session.dispatch_request(apply_req)

    assert isinstance(apply_result, JsonRpcResult)
    assert apply_result.result["files_changed"] == [str(target)]
    assert target.read_text(encoding="utf-8") == "line1\nline2-modified\nline3\n"


@pytest.mark.asyncio
async def test_v2_diff_apply_skips_rejected_hunks(tmp_path):
    """Rejected hunks must not modify the file."""
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    diff_text = (
        f"--- {target}\n"
        f"+++ {target}\n"
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "-beta\n"
        "+BETA\n"
        " gamma\n"
    )

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")

    review_req = JsonRpcRequest(
        id=30, method="diff.review", params={"diff": diff_text},
    )
    review_result = await session.dispatch_request(review_req)
    review_id = review_result.result["review_id"]

    # Reject the hunk
    decide_req = JsonRpcRequest(
        id=31, method="diff.decide",
        params={"review_id": review_id, "file_path": str(target),
                "hunk_index": 0, "decision": "rejected"},
    )
    await session.dispatch_request(decide_req)

    apply_req = JsonRpcRequest(
        id=32, method="diff.apply", params={"review_id": review_id},
    )
    apply_result = await session.dispatch_request(apply_req)

    assert isinstance(apply_result, JsonRpcResult)
    assert apply_result.result["files_changed"] == []
    # File unchanged
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


# ── diff.generate ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_diff_generate_returns_unified_diff(tmp_path):
    """diff.generate runs git diff in the workspace and returns unified diff text."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "tracked.txt").write_text("line1\nline2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "tracked.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    request = JsonRpcRequest(id=40, method="diff.generate", params={})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    diff_text = result.result["diff"]
    assert "+line3" in diff_text
    assert "tracked.txt" in diff_text


@pytest.mark.asyncio
async def test_v2_diff_generate_empty_repo_returns_empty_diff(tmp_path):
    """diff.generate on a clean repo returns empty diff string."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "clean.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    request = JsonRpcRequest(id=41, method="diff.generate", params={})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result["diff"] == ""


@pytest.mark.asyncio
async def test_v2_diff_generate_non_git_repo_returns_empty(tmp_path):
    """diff.generate in a non-git directory returns empty diff without error."""
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    request = JsonRpcRequest(id=42, method="diff.generate", params={})
    result = await session.dispatch_request(request)

    assert isinstance(result, JsonRpcResult)
    assert result.result["diff"] == ""