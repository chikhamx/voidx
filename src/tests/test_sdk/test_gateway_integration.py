from uuid import uuid4
"""Real SDK graph + file tool, answered through Gateway session.respond."""
import asyncio
import json

import pytest

from test_headless_runtime import FileRoundTripModel
from voidx.bootstrap.semantic_gateway import SemanticGatewayBridge
from voidx.config import Config, PermissionMode, Settings
from voidx.llm.adapters import langchain_model_factory
from voidx.sdk import VoidxAgent
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events.consumers import DockEventConsumer
from voidx.presentation.output.dock.status import PERMISSION_REQUEST_STATUS_ID
from voidx.agent.adapters.persistence.session_repository import load_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["allow", "deny", "cancel"])
async def test_real_gateway_permission(tmp_path, monkeypatch, answer):
    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    notifications = []
    events = []

    class Client:
        async def send_text(self, text, **kwargs):
            message = json.loads(text)
            notifications.append(message)
            if message.get("method") == "ui.request":
                p = message["params"]
                assert not (tmp_path / "sdk-probe.txt").exists()
                assert bridge.interactions.pending_count == 1
                assert dock.status_record(PERMISSION_REQUEST_STATUS_ID) is not None
                owner = bridge._thread_id
                with pytest.raises(RuntimeError, match="active"):
                    await bridge.run("must not start")
                assert bridge._thread_id == owner
                assert bridge.interactions.pending_count == 1
                if answer == "cancel":
                    await bridge.session._method_session_cancel({"thread_id": p["thread_id"]})
                else:
                    assert await bridge.session._method_session_respond({
                        "request_id": p["request_id"], "thread_id": p["thread_id"], "value": answer,
                    }) == {"ok": True}

    async with asyncio.timeout(20), bridge:
        await bridge.session.connect(Client())
        await bridge.run("Write sdk-probe.txt", workspace=str(tmp_path), observer=events.append)
    assert sum(e.kind == "interaction.required" for e in events) == 1
    resolved = [e for e in events if e.kind == "interaction.resolved"]
    assert len(resolved) == 1
    assert resolved[0].payload.resolution.decision == ("approved" if answer == "allow" else "deny")
    assert sum(e.kind in {"turn.completed", "turn.cancelled", "turn.failed"} for e in events) == 1
    assert events[-1].kind == ("turn.cancelled" if answer == "cancel" else "turn.completed")
    assert (tmp_path / "sdk-probe.txt").exists() == (answer == "allow")
    assert bridge.interactions.pending_count == 0
    assert sum(n.get("method") == "ui.request" for n in notifications) == 1

    def nodes(node):
        yield node
        for child in node.children:
            yield from nodes(child)
    kinds = {node.node_type for node in nodes(dock.tree.root)}
    assert "turn" in kinds
    if answer == "allow":
        assert {"assistant", "tool_call", "tool_result"} <= kinds
        rows = await load_messages(events[0].session_id)
        assert {"user", "assistant", "tool"} <= {row.role for row in rows}
        assert any("FILE_WRITTEN" in str(row.content) for row in rows)
    assert dock.status_record(PERMISSION_REQUEST_STATUS_ID) is None


@pytest.mark.asyncio
async def test_close_waits_for_owned_run():
    from voidx.agent.domain.semantic_events import SemanticEvent

    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class Agent:
        async def cancel(self):
            raise AssertionError("unexpected cancellation")

        async def __aenter__(self):
            return self

        async def stream(self, *args, **kwargs):
            entered.set()
            await release.wait()
            finished.set()
            if False:
                yield SemanticEvent

        async def aclose(self):
            release.set()

        async def submit_interaction(self, *args):
            raise AssertionError("unexpected interaction")

    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(Agent(), consumer=DockEventConsumer(dock), tree=dock.tree)
    task = asyncio.create_task(bridge.run("wait"))
    try:
        await entered.wait()
        await bridge.__aexit__(None, None, None)
        assert finished.is_set(), "close must await the managed bridge run"
        assert task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("run_fails", [False, True])
async def test_close_failure_still_awaits_run(run_fails):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Agent:
        async def cancel(self):
            raise AssertionError("unexpected cancellation")

        async def stream(self, *args, **kwargs):
            entered.set()
            await release.wait()
            await asyncio.sleep(0)
            finished.set()
            if run_fails:
                raise ValueError("run failed")
            if False:
                yield

        async def aclose(self):
            release.set()
            raise OSError("close failed")

        async def submit_interaction(self, *args):
            return True

    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(Agent(), consumer=DockEventConsumer(dock), tree=dock.tree)
    task = asyncio.create_task(bridge.run("wait"))
    try:
        await entered.wait()
        with pytest.raises(BaseException) as caught:
            await bridge.__aexit__(None, None, None)
        assert finished.is_set() and task.done()
        if run_fails:
            assert isinstance(caught.value, ExceptionGroup)
            assert [str(e) for e in caught.value.exceptions] == ["close failed", "run failed"]
        else:
            assert isinstance(caught.value, OSError)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_bridge_sequential_runs_have_fresh_routes():
    from voidx.agent.domain import semantic_events as e
    from voidx.tooling.domain.interaction import InteractionRequest

    class Agent:
        async def cancel(self):
            raise AssertionError("unexpected cancellation")

        async def stream(self, *args, **kwargs):
            identity = dict(session_id="s", thread_id="t", turn_id="turn")
            request = InteractionRequest(**identity, interaction_id="permission", input_kind="permission",
                purpose="permission", prompt="Allow?", choices=[dict(label="Allow", value="allow")])
            from voidx.presentation.protocol.requests import UiPermissionRequest
            bridge.interactions.register(request, UiPermissionRequest(request_id="permission", thread_id="t", prompt="?"))
            if False:
                yield

        async def submit_interaction(self, *args):
            return True

    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(Agent(), consumer=DockEventConsumer(dock), tree=dock.tree)
    await bridge.run("first")
    previous = bridge.interactions
    await bridge.run("second")
    assert bridge.interactions is not previous
    assert bridge.session._interaction_router is bridge.interactions
    assert bridge.interactions.pending_count == 0


@pytest.mark.asyncio
async def test_real_sdk_reconnect_failure_cleanup_and_next_turn(tmp_path, monkeypatch):
    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    waiting = asyncio.Event()
    events = []

    def observe(event):
        events.append(event)
        if event.kind == "interaction.required":
            waiting.set()

    class Broken:
        async def send_text(self, text, **kwargs):
            if json.loads(text).get("method") == "ui.request":
                raise OSError("reconnect failed")

    async with asyncio.timeout(20), bridge:
        task = asyncio.create_task(bridge.run("Write sdk-probe.txt", workspace=str(tmp_path), observer=observe))
        await waiting.wait()
        assert bridge.interactions.pending_count == 1
        assert not bridge.session._run_manager.actor(bridge._thread_id).state.pending_requests
        with pytest.raises(OSError, match="reconnect failed"):
            await bridge.session.connect(Broken())
        await task
        assert events[-1].kind == "turn.cancelled"
        assert sum(e.kind == "interaction.resolved" for e in events) == 1
        assert not (tmp_path / "sdk-probe.txt").exists()
        first_route = bridge.interactions
        second_events = []

        class Client:
            async def send_text(self, text, **kwargs):
                message = json.loads(text)
                if message.get("method") == "ui.request":
                    p = message["params"]
                    assert await bridge.session._method_session_respond(dict(
                        request_id=p["request_id"], thread_id=p["thread_id"], value="allow")) == {"ok": True}

        client = Client()
        await bridge.session.connect(client)
        await bridge.run("Write sdk-probe.txt", workspace=str(tmp_path), observer=second_events.append)
        bridge.session.disconnect(client)
        if bridge.session._persisted_sync_task is not None:
            await bridge.session._persisted_sync_task
        assert bridge.interactions is not first_route
        assert second_events[-1].kind == "turn.completed"
        assert sum(e.kind == "interaction.required" for e in second_events) == 1
        assert (tmp_path / "sdk-probe.txt").read_text() == "headless\n"


@pytest.mark.asyncio
async def test_real_gateway_permission_shared_fixture(tmp_path, monkeypatch):
    from pathlib import Path

    from langchain_core.messages import AIMessage

    class HistoryModel(FileRoundTripModel):
        def _reply(self, messages):
            response = super()._reply(messages)
            if response.content == "FILE_WRITTEN":
                return AIMessage(content="FILE_WRITTEN\n\nHistory body")
            return response

    model = HistoryModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    ready = asyncio.Event()
    live, pending, fresh = [], [], []

    class Client:
        def __init__(self, messages):
            self.messages = messages

        async def send_text(self, text, **kwargs):
            message = json.loads(text)
            self.messages.append(message)
            if message.get("method") == "ui.request":
                ready.set()

    async with asyncio.timeout(20), bridge:
        await bridge.session.connect(Client(live))
        task = asyncio.create_task(bridge.run("Write sdk-probe.txt", workspace=str(tmp_path)))
        try:
            await ready.wait()
            assert not (tmp_path / "sdk-probe.txt").exists()
            await bridge.session.connect(Client(pending))
            request = next(m["params"] for m in live if m.get("method") == "ui.request")
            assert request.get("response_method") == "session.respond"
            assert request.get("reconnect_policy") == "replace"
            pending_at_connect = list(pending)
            assert await bridge.session._method_session_respond({
                "request_id": request["request_id"], "thread_id": request["thread_id"],
                "value": "allow",
            }) == {"ok": True}
            await task
            await bridge.session.connect(Client(fresh))
        finally:
            if not task.done():
                await bridge.agent.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert (tmp_path / "sdk-probe.txt").read_text() == "headless\n"
    assert bridge.interactions.pending_count == 0
    assert dock.status_record(PERMISSION_REQUEST_STATUS_ID) is None
    snapshots = [m for m in fresh if m.get("method") == "workspace.snapshot"]
    assert len(snapshots) == 1
    snapshot = snapshots[0]["params"]["active_snapshot"]
    assert snapshot is not None
    assert snapshot["nodes"], "Explicit SDK snapshot must include the real Dock tree"
    assert not any(node["payload"].get("prompt_type") == "permission"
                   for node in snapshot["nodes"])

    assistant_text = [n for n in snapshot["nodes"]
                      if n["payload"].get("raw_text") == "FILE_WRITTEN\n\nHistory body"]
    assert len(assistant_text) == 1, "Real graph assistant text must survive in Gateway history"
    assert assistant_text[0]["body_lines"], "History must contain rendered assistant body"

    def relevant(messages):
        return [m for m in messages if m.get("method") in {"ui.request", "workspace.snapshot"} or
                (m.get("method") in {"item.started", "item.completed"} and
                 m.get("params", {}).get("kind") == "prompt")]

    actual = {"live": [m for m in relevant(live) if m["method"] != "workspace.snapshot"], "pending_reconnect": relevant(pending_at_connect),
              "fresh_reconnect": relevant(fresh)}
    assert [m["method"] for m in actual["live"]] == ["ui.request", "item.completed"]
    assert [m["method"] for m in actual["pending_reconnect"]] == ["workspace.snapshot", "ui.request"]
    assert [m["method"] for m in actual["fresh_reconnect"]] == ["workspace.snapshot"]
    completed = actual["live"][1]["params"]
    assert completed["data"] == {
        "prompt_type": "permission", "request_id": request["request_id"], "cleared": True,
    }
    assert completed["thread_id"] == request["thread_id"]
    identities = {
        request["request_id"]: "request-1", request["thread_id"]: "thread-1",
        completed["turn_id"]: "turn-1", completed["item_id"]: "item-1",
    }
    assert len(identities) == 4

    for phase in ("pending_reconnect", "fresh_reconnect"):
        epoch = actual[phase][0]["params"]["active_snapshot"]["transcript_epoch"]
        identities.setdefault(epoch, f"epoch-{len([v for v in identities.values() if v.startswith('epoch-')]) + 1}")
    nodes = snapshot["nodes"]
    assert {"turn", "assistant", "tool_call", "tool_result"} <= {n["node_type"] for n in nodes}
    call = next(n for n in nodes if n["node_type"] == "tool_call")
    result = next(n for n in nodes if n["node_type"] == "tool_result")
    assert call["payload"]["raw_args"] == {
        "file_path": "sdk-probe.txt", "op": "write", "new_string": "headless\n"}
    assert result["tool_call_id"] == call["tool_call_id"] == "write-probe"
    assert result["payload"]["result_anchor_id"] == call["id"]
    assert result["payload"]["raw_text"] == "File created"

    def normalize(value):
        if isinstance(value, dict):
            return {k: (0.0 if k == "elapsed" and v is not None else normalize(v))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, str):
            return identities.get(value, value.replace(str(tmp_path), "<workspace>"))
        return value

    actual = normalize(actual)
    expected = json.loads((Path(__file__).resolve().parents[3] /
                           "frontend/test/fixtures/semantic-permission.json").read_text())
    # The activation snapshot adds one workspace revision and one snapshot sequence.
    for phase in ("pending_reconnect", "fresh_reconnect"):
        expected[phase][0]["params"]["revision"] += 1
        expected[phase][0]["params"]["active_snapshot"]["revision"] += 1
    assert actual == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["allow", "deny", None], ids=["allow", "deny", "cancel"])
async def test_real_websocket_permission_reconnect_and_sequential(tmp_path, monkeypatch, answer):
    import websockets
    from voidx.presentation.gateway.server import GatewayServer

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    session = bridge.session
    server = GatewayServer(session, host="127.0.0.1", port=0)
    notifications = []
    rpc_id = 0
    run = None

    async def receive(socket):
        message = json.loads(await socket.recv())
        if "method" in message:
            notifications.append(message)
        return message

    async def respond(socket, request, *, value, thread_id=None):
        nonlocal rpc_id
        rpc_id += 1
        await socket.send(json.dumps({"jsonrpc": "2.0", "id": rpc_id,
            "method": "session.respond", "params": {
                "request_id": request["request_id"],
                "thread_id": request["thread_id"] if thread_id is None else thread_id,
                "value": value}}))
        while True:
            message = await receive(socket)
            if message.get("id") == rpc_id:
                assert "error" not in message
                return message["result"]

    def assert_no_legacy_futures():
        assert all(not actor.state.pending_requests
                   for actor in session._run_manager._actors.values())

    async with asyncio.timeout(40), bridge:
        await server.start()
        try:
            async with websockets.connect(server.url) as socket:
                assert (await receive(socket))["method"] == "workspace.snapshot"
                events = []
                run = asyncio.create_task(bridge.run("Write sdk-probe.txt",
                    workspace=str(tmp_path), observer=events.append))
                while True:
                    message = await receive(socket)
                    if message.get("method") == "ui.request":
                        request = message["params"]
                        break
                assert not run.done()
                assert not (tmp_path / "sdk-probe.txt").exists()
                assert bridge.interactions.pending_count == 1
                assert_no_legacy_futures()
                assert dock.status_record(PERMISSION_REQUEST_STATUS_ID) is not None
                owner = bridge._thread_id
                with pytest.raises(RuntimeError, match="active"):
                    await bridge.run("must not overlap")
                assert bridge._thread_id == owner
                assert await respond(socket, request, value="allow", thread_id="wrong-thread") == {"ok": False}
                assert await respond(socket, request, value="allow", thread_id="") == {"ok": False}
                assert bridge.interactions.pending_count == 1

            async with websockets.connect(server.url) as socket:
                snapshot = await receive(socket)
                replay = await receive(socket)
                assert snapshot["method"] == "workspace.snapshot"
                assert replay["method"] == "ui.request"
                assert replay["params"] == request
                assert not (tmp_path / "sdk-probe.txt").exists()
                assert await respond(socket, request, value=answer) == {"ok": True}
                await run
                assert await respond(socket, request, value="allow") == {"ok": False}
                resolved = [e for e in events if e.kind == "interaction.resolved"]
                assert len(resolved) == 1
                assert resolved[0].payload.resolution.decision == ("approved" if answer == "allow" else "deny")
                assert sum(e.kind == "interaction.required" for e in events) == 1
                assert sum(e.kind in {"turn.completed", "turn.cancelled", "turn.failed"} for e in events) == 1
                assert events[-1].kind == "turn.completed"
                target = tmp_path / "sdk-probe.txt"
                assert target.exists() == (answer == "allow")
                if answer == "allow":
                    assert target.read_text() == "headless\n"
                assert bridge.interactions.pending_count == 0
                assert dock.status_record(PERMISSION_REQUEST_STATUS_ID) is None
                assert_no_legacy_futures()
                assert sum(n.get("method") == "ui.request" for n in notifications) == 2

                second_events = []
                await bridge.run("RESTORE_PROBE", session_id=events[0].session_id,
                    workspace=str(tmp_path), observer=second_events.append)
                assert bridge.session is session
                assert second_events[0].session_id == events[0].session_id
                assert second_events[0].turn_id != events[0].turn_id
                assert sum(e.kind in {"turn.completed", "turn.cancelled", "turn.failed"}
                           for e in second_events) == 1
                assert second_events[-1].kind == "turn.completed"
                assert await respond(socket, request, value="allow") == {"ok": False}
                assert bridge.interactions.pending_count == 0
                assert_no_legacy_futures()
        finally:
            if run is not None and not run.done():
                await bridge.agent.cancel()
                await run
            await server.stop()
        assert not session.clients


@pytest.mark.asyncio
async def test_real_websocket_session_cancel_pending_permission(tmp_path, monkeypatch):
    import websockets
    from voidx.presentation.gateway.server import GatewayServer

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    session = bridge.session
    server = GatewayServer(session, host="127.0.0.1", port=0)
    notifications, events = [], []
    rpc_id = 0
    run = None
    target = tmp_path / "sdk-probe.txt"

    async def receive(socket):
        message = json.loads(await socket.recv())
        if "method" in message:
            notifications.append(message)
        return message

    async def rpc(socket, method, params):
        nonlocal rpc_id
        rpc_id += 1
        await socket.send(json.dumps({"jsonrpc": "2.0", "id": rpc_id,
                                      "method": method, "params": params}))
        while True:
            message = await receive(socket)
            if message.get("id") == rpc_id:
                return message

    def assert_no_legacy_futures():
        assert all(not actor.state.pending_requests
                   for actor in session._run_manager._actors.values())

    async with asyncio.timeout(40), bridge:
        await server.start()
        try:
            async with websockets.connect(server.url) as socket:
                assert (await receive(socket))["method"] == "workspace.snapshot"
                run = asyncio.create_task(bridge.run("Write sdk-probe.txt",
                    workspace=str(tmp_path), observer=events.append))
                while True:
                    message = await receive(socket)
                    if message.get("method") == "ui.request":
                        request = message["params"]
                        break
                managed = bridge.agent._run
                coordinator = bridge.agent._interactions
                assert managed is not None and coordinator is not None
                pending = coordinator._pending[request["request_id"]]
                future = pending.future
                assert future is not None and not future.done()
                assert bridge.interactions.pending_count == 1
                assert not run.done() and not target.exists()
                assert dock.status_record(PERMISSION_REQUEST_STATUS_ID) is not None
                assert_no_legacy_futures()

                rejected = await rpc(socket, "session.cancel", {"thread_id": "wrong-thread"})
                assert "result" not in rejected
                assert rejected["error"]["code"] == -32603
                assert rejected["error"]["message"] == "internal error"
                assert not managed.cancelling and not run.done()
                assert bridge._thread_id == request["thread_id"]
                assert coordinator._pending[request["request_id"]] is pending
                assert pending.future is future and not future.done()
                assert bridge.interactions.pending_count == 1
                assert dock.status_record(PERMISSION_REQUEST_STATUS_ID) is not None
                assert not target.exists()
                assert not any(e.kind in {"interaction.resolved", "turn.completed",
                                          "turn.cancelled", "turn.failed"} for e in events)

                cancelled = await rpc(socket, "session.cancel", {"thread_id": request["thread_id"]})
                assert "error" not in cancelled
                assert cancelled["result"] == {"ok": True}
                await run
                stale = await rpc(socket, "session.respond", {
                    "request_id": request["request_id"],
                    "thread_id": request["thread_id"], "value": "allow"})
                assert "error" not in stale
                assert stale["result"] == {"ok": False}
                assert sum(e.kind == "interaction.required" for e in events) == 1
                resolved = [e for e in events if e.kind == "interaction.resolved"]
                assert len(resolved) == 1
                assert resolved[0].payload.interaction_id == request["request_id"]
                assert resolved[0].payload.resolution.decision == "deny"
                terminal = [e for e in events if e.kind in {
                    "turn.completed", "turn.cancelled", "turn.failed"}]
                assert len(terminal) == 1 and terminal[0].kind == "turn.cancelled"
                assert events[-1] is terminal[0]
                assert resolved[0].thread_id == terminal[0].thread_id == request["thread_id"]
                assert resolved[0].turn_id == terminal[0].turn_id
                assert not target.exists()
                assert bridge.interactions.pending_count == 0
                assert not coordinator._pending
                assert future.done() and pending.future is None
                assert_no_legacy_futures()
                assert dock.status_record(PERMISSION_REQUEST_STATUS_ID) is None
                assert managed.cancelling
                assert managed._task is not None and managed._task.done()
                assert managed._work is not None and managed._work.done()
                assert managed.channel.completion.done()
                assert run.done() and run.exception() is None
                assert bridge._run_task is None and not bridge._active
                assert bridge.agent._run is None and bridge.agent._interactions is None
                assert not bridge.agent._active

            async with websockets.connect(server.url) as socket:
                snapshot = await receive(socket)
                assert snapshot["method"] == "workspace.snapshot"
                active = snapshot["params"]["active_snapshot"]
                assert active is not None and active["nodes"]
                assert not any(node["payload"].get("prompt_type") == "permission"
                               for node in active["nodes"])
                # An RPC response is a wire barrier after any connect-time replay.
                stale = await rpc(socket, "session.respond", {
                    "request_id": request["request_id"],
                    "thread_id": request["thread_id"], "value": "allow"})
                assert "error" not in stale and stale["result"] == {"ok": False}
                assert sum(n.get("method") == "ui.request" for n in notifications) == 1
                assert bridge.interactions.pending_count == 0
                assert not coordinator._pending and not target.exists()
                assert_no_legacy_futures()
        finally:
            if run is not None and not run.done():
                await bridge.agent.cancel()
                await run
            await server.stop()
        assert not session.clients


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_first", [True, False])
@pytest.mark.parametrize("reuse_thread", [True, False])
@pytest.mark.parametrize("capabilities", [[], ["transcript_window_v1"]])
async def test_real_gateway_thread_activation_order(tmp_path, monkeypatch, connect_first, reuse_thread, capabilities):
    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    notifications, events = [], []

    class Client:
        async def send_text(self, text, **kwargs):
            notifications.append(json.loads(text))

    client = Client()
    run = None
    async with asyncio.timeout(30), bridge:
        try:
            if connect_first:
                await bridge.session.connect(client, capabilities=capabilities)
            first_session = ""
            previous_adapter = None
            for turn in range(2):
                notifications.clear()
                events.clear()
                session_id = first_session if turn and reuse_thread else ""
                run = asyncio.create_task(bridge.run("Write sdk-probe.txt", session_id=session_id,
                    workspace=str(tmp_path), observer=events.append))
                while not bridge.interactions.pending_count and not run.done():
                    await asyncio.sleep(0.01)
                tid = events[0].thread_id
                if not turn:
                    first_session = events[0].session_id
                assert bridge.session._active_thread_id == tid
                assert tid in bridge.session._threads
                if not connect_first and not turn:
                    await bridge.session.connect(client, capabilities=capabilities)
                thread_messages = [n for n in notifications if n.get("params", {}).get("thread_id") == tid]
                snapshots = [n for n in notifications if n.get("method") == "workspace.snapshot"]
                assert snapshots, "turn activation must publish the real Dock snapshot before business notifications"
                activation = snapshots[0]
                assert activation["params"]["active_thread_id"] == tid
                assert activation["params"]["active_snapshot"]["thread_id"] == tid
                assert activation["params"]["active_snapshot"]["nodes"]
                assert any(t["thread_id"] == tid for t in activation["params"]["threads"])
                assert thread_messages
                assert notifications.index(activation) < notifications.index(thread_messages[0])
                adapter = bridge.session._adapters[tid]
                assert adapter is not previous_adapter
                previous_adapter = adapter
                requests = [n["params"] for n in notifications if n.get("method") == "ui.request"]
                if requests:
                    assert len(requests) == 1
                    request = requests[0]
                    assert await bridge.session._method_session_respond({
                        "request_id": request["request_id"], "thread_id": tid, "value": "allow",
                    }) == {"ok": True}
                await run
                assert events[-1].kind == "turn.completed"
                assert sum(n.get("method") == "turn.started" for n in notifications) <= 1
                assert bridge.interactions.pending_count == 0
                assert not adapter._stream_items
                assert all(not actor.state.pending_requests
                           for actor in bridge.session._run_manager._actors.values())
            assert (tmp_path / "sdk-probe.txt").read_text() == "headless\n"
        finally:
            if run is not None and not run.done():
                await bridge.agent.cancel()
                await run
            bridge.session.disconnect(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("manage_capture", [True, False])
@pytest.mark.parametrize("outcome", ["normal", "error", "cancel"])
async def test_capture_ownership_and_finally(manage_capture, outcome, monkeypatch):
    from voidx.presentation.output.events.schema import CaptureStarted, CaptureStopped

    dock = BottomInputDock()
    monkeypatch.setattr(dock, "activate", lambda: pytest.fail("capture must not activate Live"))
    if not manage_capture:
        dock.begin_capture()
    calls = []

    class Consumer(DockEventConsumer):
        def handle(self, event):
            calls.append(type(event))
            return super().handle(event)

        async def drain_stream_commits(self):
            calls.append("drain")
            await super().drain_stream_commits()

    class Agent:
        async def cancel(self):
            pass

        async def stream(self, *args, **kwargs):
            assert dock.active
            if outcome == "error":
                raise ValueError("run failed")
            if outcome == "cancel":
                raise asyncio.CancelledError()
            if False:
                yield

        async def submit_interaction(self, *args):
            return True

    bridge = SemanticGatewayBridge(Agent(), consumer=Consumer(dock), tree=dock.tree,
                                   manage_capture=manage_capture)
    if outcome == "normal":
        await bridge.run("capture")
    else:
        with pytest.raises(ValueError if outcome == "error" else asyncio.CancelledError):
            await bridge.run("capture")
    assert dock.active is (not manage_capture)
    assert calls == ([CaptureStarted, "drain", CaptureStopped] if manage_capture else ["drain"])
    assert not bridge._active


@pytest.mark.asyncio
async def test_capture_cleanup_aggregates_run_drain_stop_errors():
    from voidx.presentation.output.events.schema import CaptureStopped

    calls = []

    class Consumer:
        def handle(self, event):
            if isinstance(event, CaptureStopped):
                calls.append("stop")
                raise OSError("stop failed")

        async def drain_stream_commits(self):
            calls.append("drain")
            raise RuntimeError("drain failed")

    class Agent:
        async def cancel(self):
            pass

        async def stream(self, *args, **kwargs):
            raise ValueError("run failed")
            yield

        async def submit_interaction(self, *args):
            return True

    bridge = SemanticGatewayBridge(Agent(), consumer=Consumer(), tree=BottomInputDock().tree)
    with pytest.raises(ExceptionGroup) as caught:
        await bridge.run("capture")
    assert [str(e) for e in caught.value.exceptions] == ["run failed", "drain failed", "stop failed"]
    assert calls == ["drain", "stop"]
    assert not bridge._active


class ClarifyRoundTripModel(FileRoundTripModel):
    options: list[str] = ["Staging", "Production"]

    def _reply(self, messages):
        from langchain_core.messages import AIMessage, ToolMessage
        self._histories.append(list(messages))
        if any(isinstance(m, ToolMessage) and m.tool_call_id == "clarify-probe" for m in messages):
            return AIMessage(content="CLARIFY_DONE")
        return AIMessage(content="", tool_calls=[{
            "id": "clarify-probe", "name": "clarify",
            "args": {"question": "Which environment?", "options": self.options},
        }])


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["Staging", "custom environment", None, "no", "deny", "reject", "rejected"])
async def test_real_gateway_clarify(tmp_path, monkeypatch, answer):
    from langchain_core.messages import ToolMessage
    options = [answer, "Production"] if answer in {"no", "deny", "reject", "rejected"} else ["Staging", "Production"]
    model = ClarifyRoundTripModel(options=options)
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE, lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    events, requests = [], []

    class Client:
        async def send_text(self, text, **kwargs):
            message = json.loads(text)
            if message.get("method") == "ui.request":
                p = message["params"]
                requests.append(p)
                assert p["prompt"] == "Which environment?"
                assert [c[1] for c in p["choices"]] == options
                assert await bridge.session._method_session_respond({
                    "request_id": p["request_id"], "thread_id": p["thread_id"], "value": answer,
                }) == {"ok": True}

    async with asyncio.timeout(20), bridge:
        await bridge.session.connect(Client())
        await bridge.run("Clarify the environment", workspace=str(tmp_path), observer=events.append)
    required = [e for e in events if e.kind == "interaction.required"]
    assert len(required) == len(requests) == 1
    assert required[0].payload.request.purpose == "clarify"
    resolved = [e for e in events if e.kind == "interaction.resolved"]
    assert len(resolved) == 1
    resolution = resolved[0].payload.resolution
    assert resolution.decision == ("skipped" if answer is None else "answered")
    assert resolution.value == (answer or "")
    assert resolution.free_text == (answer == "custom environment")
    assert resolution.resolution_reason == ("dismissed" if answer is None else "answered")
    assert sum(e.kind in {"turn.completed", "turn.cancelled", "turn.failed"} for e in events) == 1
    assert events[-1].kind == "turn.completed"
    assert bridge.interactions.pending_count == 0
    tool_messages = [m for h in model._histories for m in h if isinstance(m, ToolMessage)]
    expected = answer if answer is not None else "User skipped clarification"
    assert any(expected in str(m.content) for m in tool_messages)
    rows = await load_messages(events[0].session_id)
    assert any(row.role == "tool" and expected in str(row.content) for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancel", "timeout", "answered", "rejected", "dismissed"])
async def test_real_gateway_clarify_lifecycle_and_sequential_reuse(tmp_path, monkeypatch, outcome):
    from langchain_core.messages import ToolMessage
    from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
    from voidx.tooling.domain.interaction import InteractionResponse

    model = ClarifyRoundTripModel(options=["no", "Production"])
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    if outcome == "timeout":
        original_request = InteractionCoordinator.request

        async def short_request(self, request):
            if request.purpose == "clarify":
                request = request.model_copy(update={"timeout": 0.05})
            return await original_request(self, request)

        monkeypatch.setattr(InteractionCoordinator, "request", short_request)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE, lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    requests = asyncio.Queue()

    class Client:
        async def send_text(self, text, **kwargs):
            message = json.loads(text)
            if message.get("method") == "ui.request":
                requests.put_nowait(message["params"])

    previous_route = previous_request = None
    async with asyncio.timeout(20), bridge:
        client = Client()
        await bridge.session.connect(client)
        for action in (outcome, "answered"):
            events = []
            model._histories.clear()
            run = asyncio.create_task(bridge.run("Clarify the environment", workspace=str(tmp_path), observer=events.append))
            try:
                request = await requests.get()
                route = bridge.interactions
                assert route is not previous_route
                assert bridge.session._interaction_router is route
                managed = bridge.agent._run
                coordinator = bridge.agent._interactions
                pending = coordinator._pending[request["request_id"]]
                future = pending.future
                assert future is not None and not future.done()
                assert route.pending_count == 1
                if previous_request is not None:
                    assert request["request_id"] != previous_request["request_id"]
                    assert await bridge.session._method_session_respond({**previous_request, "value": "no"}) == {"ok": False}
                    assert not future.done()
                if action == "cancel":
                    assert await bridge.session._method_session_cancel({"thread_id": request["thread_id"]}) == {"ok": True}
                elif action in {"rejected", "dismissed"}:
                    assert await bridge.agent.submit_interaction(request["request_id"], InteractionResponse(
                        **coordinator._identity, value="no", rejected=action == "rejected", cancelled=action == "dismissed"))
                elif action == "answered":
                    assert await bridge.session._method_session_respond({**request, "value": "no"}) == {"ok": True}
                await run
                required = [e for e in events if e.kind == "interaction.required"]
                resolved = [e for e in events if e.kind == "interaction.resolved"]
                terminal = [e for e in events if e.kind in {"turn.completed", "turn.cancelled", "turn.failed"}]
                assert len(required) == len(resolved) == len(terminal) == 1
                assert required[0].payload.request.purpose == "clarify"
                assert resolved[0].payload.interaction_id == request["request_id"]
                resolution = resolved[0].payload.resolution
                assert resolution.decision == ("answered" if action == "answered" else "skipped")
                assert resolution.resolution_reason == {
                    "cancel": "task_cancelled", "timeout": "timed_out", "answered": "answered",
                    "rejected": "user_rejected", "dismissed": "dismissed",
                }[action]
                assert terminal[0].kind == ("turn.cancelled" if action == "cancel" else "turn.completed")
                assert events[-1] is terminal[0]
                assert resolved[0].turn_id == required[0].turn_id == terminal[0].turn_id
                assert resolved[0].thread_id == terminal[0].thread_id == request["thread_id"]
                assert not coordinator._pending and future.done() and pending.future is None
                assert route.pending_count == 0 and not dock.active
                assert managed._task.done() and managed._work.done() and managed.channel.completion.done()
                assert bridge.agent._run is None and bridge.agent._interactions is None
                assert not bridge.agent._active and not bridge._active and bridge._run_task is None
                assert await bridge.session._method_session_respond({**request, "value": "no"}) == {"ok": False}
                if action != "cancel":
                    expected = '"answer": "no"' if action == "answered" else "User skipped clarification"
                    assert any(isinstance(m, ToolMessage) and expected in str(m.content) for h in model._histories for m in h)
                    rows = await load_messages(events[0].session_id)
                    assert any(row.role == "tool" and expected in str(row.content) for row in rows)
                previous_route, previous_request = route, request
            finally:
                if not run.done():
                    await bridge.agent.cancel()
                    await run
        bridge.session.disconnect(client)
    assert not bridge.session.clients


class CheckpointRoundTripModel(FileRoundTripModel):
    def _reply(self, messages):
        from langchain_core.messages import AIMessage, ToolMessage
        self._histories.append(list(messages))
        if any(isinstance(m, ToolMessage) and m.tool_call_id == "checkpoint-probe" for m in messages):
            return AIMessage(content="CHECKPOINT_DONE")
        return AIMessage(content="", tool_calls=[{
            "id": "checkpoint-probe", "name": "checkpoint",
            "args": {"goal": "Wire checkpoint", "steps": ["Test", "Implement"]},
        }])


@pytest.mark.asyncio
@pytest.mark.parametrize("answers,decision", [
    (["approved"], "approved"), (["needs_doc"], "needs_doc"),
    (["rejected"], "rejected"), ([None], "rejected"),
    (["custom scope"], "modified"), (["modified", "approved"], "modified"),
    (["modified", "no"], "modified"), (["modified", None], "rejected"),
])
async def test_real_gateway_checkpoint(tmp_path, monkeypatch, answers, decision):
    from langchain_core.messages import ToolMessage
    model = CheckpointRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE, lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    displayed = []

    class RecordingConsumer(DockEventConsumer):
        def handle(self, event):
            displayed.append(event)
            return super().handle(event)

    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=RecordingConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    events, requests = [], []
    clients = []

    class Client:
        def __init__(self, *, responds=False):
            self.responds = responds
            self.messages = []
            self.card_ids = []

        async def send_text(self, text, **kwargs):
            message = json.loads(text)
            self.messages.append(message)
            if message.get("method") == "ui.request":
                p = message["params"]
                snapshot = self.messages[-2]
                assert snapshot["method"] == "workspace.snapshot"
                active = snapshot["params"]["active_snapshot"]
                cards = [n for n in active["nodes"] if n["node_type"] == "checkpoint"]
                assert len(cards) == 1, "live Dock card must precede ui.request"
                self.card_ids.append(cards[0]["id"])
                assert len(set(self.card_ids)) == 1
                if self.responds:
                    requests.append(p)
                    assert await bridge.session._method_session_respond({
                        "request_id": p["request_id"], "thread_id": p["thread_id"],
                        "value": answers[len(requests) - 1],
                    }) == {"ok": True}

    async with asyncio.timeout(10), bridge:
        for capabilities in ([], ["workspace_patch_v1", "transcript_window_v1"]):
            client = Client(responds=not clients)
            clients.append(client)
            await bridge.session.connect(client, capabilities=capabilities)
        await bridge.run("Present checkpoint", workspace=str(tmp_path), observer=events.append)
    for client in clients:
        snapshots = [m["params"]["active_snapshot"] for m in client.messages
                     if m.get("method") == "workspace.snapshot"]
        revisions = [s["revision"] for s in snapshots]
        assert revisions == sorted(set(revisions))
        assert len(client.card_ids) == len(answers)
        decisions = [n for s in snapshots for n in s["nodes"]
                     if n["node_type"] == "checkpoint" and n["payload"].get("decision")]
        assert decisions and all(n["id"] == client.card_ids[0] for n in decisions)
        assert decisions[-1]["payload"]["decision"] == decision
        assert not any(m.get("method") == "item.completed"
                       and m["params"].get("data", {}).get("prompt_type") == "checkpoint"
                       for m in client.messages), "resolver must update the original Dock card"
    required = [e.payload.request for e in events if e.kind == "interaction.required"]
    resolved = [e.payload.resolution for e in events if e.kind == "interaction.resolved"]
    assert len(required) == len(requests) == len(answers)
    assert all(r.purpose == "checkpoint" for r in required)
    assert required[0].interaction_id == required[0].checkpoint_id
    if len(required) == 2:
        assert required[1].checkpoint_id == required[0].checkpoint_id
        assert required[1].interaction_id != required[0].interaction_id
        assert required[1].checkpoint_stage == "scope"
    shown = [e for e in displayed if e.kind == "checkpoint_prompt.shown"]
    submitted = [e for e in displayed if e.kind == "checkpoint_decision.submitted"]
    assert len(shown) == 1
    assert len(submitted) == len(answers)
    assert all(e.checkpoint_id == shown[0].checkpoint_id for e in submitted)
    assert [e.decision for e in submitted] == [r.decision for r in resolved]
    assert len(resolved) == len(answers)
    assert resolved[-1].decision == decision
    assert events[-1].kind == "turn.completed", {
        "terminal": events[-1].model_dump(), "decisions": [r.model_dump() for r in resolved],
        "boundary": "checkpoint execute -> state update/persist -> headless invalidate",
    }
    assert bridge.interactions.pending_count == 0
    tool_messages = [m for h in model._histories for m in h if isinstance(m, ToolMessage)]
    assert any(json.loads(m.content)["decision"] == decision for m in tool_messages)
    rows = await load_messages(events[0].session_id)
    assert any(row.role == "tool" and json.loads(row.content)["decision"] == decision for row in rows)
    from voidx.agent.adapters.persistence.runtime_state_repository import load_runtime_state
    state = (await load_runtime_state(events[0].session_id)).task_state
    if decision != "rejected":
        expected_scope = answers[-1] if decision == "modified" else "Wire checkpoint"
        assert state.current_goal.desc == expected_scope
    if decision in {"approved", "needs_doc"}:
        target = "tdd" if decision == "approved" else "design"
        assert state.workflow_route.join == target
        assert state.workflow_runs[target].status.value == "active"
        assert state.workflow_runs[target].scope == "Wire checkpoint"
        assert state.workflow_route.leave == ("verify" if decision == "approved" else "design")
    if decision == "rejected":
        assert not state.workflow_runs
        assert state.workflow_route is None
    assert len({e.event_id for e in events}) == len(events)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["decision", "scope"])
@pytest.mark.parametrize("outcome", ["dismissed", "timeout", "cancel"])
async def test_real_checkpoint_cleanup_and_reuse(tmp_path, monkeypatch, stage, outcome):
    from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
    original = InteractionCoordinator.request

    async def timed_request(self, request):
        return await original(self, request.model_copy(update={"timeout": 0.1 if outcome == "timeout" else 5}))

    monkeypatch.setattr(InteractionCoordinator, "request", timed_request)
    model = CheckpointRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE, lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    requests = asyncio.Queue()

    class Client:
        async def send_text(self, text, **kwargs):
            message = json.loads(text)
            if message.get("method") == "ui.request":
                requests.put_nowait(message["params"])

    previous = None
    all_ids = set()
    async with asyncio.timeout(20), bridge:
        await bridge.session.connect(Client())
        for action in (outcome, "answered"):
            events = []
            model._histories.clear()
            run = asyncio.create_task(bridge.run("Checkpoint", workspace=str(tmp_path), observer=events.append))
            try:
                request = await requests.get()
                coordinator = bridge.agent._interactions
                managed = bridge.agent._run
                if stage == "scope":
                    assert await bridge.session._method_session_respond({**request, "value": "modified"}) == {"ok": True}
                    request = await requests.get()
                pending = coordinator._pending[request["request_id"]]
                future = pending.future
                assert future is not None and not future.done()
                assert await bridge.session._method_session_respond({**request, "thread_id": "wrong", "value": "approved"}) == {"ok": False}
                if previous:
                    assert await bridge.session._method_session_respond({**previous, "value": "approved"}) == {"ok": False}
                assert not future.done()
                if action == "cancel":
                    assert await bridge.session._method_session_cancel({"thread_id": request["thread_id"]}) == {"ok": True}
                elif action != "timeout":
                    assert await bridge.session._method_session_respond({**request, "value": None if action == "dismissed" else "approved"}) == {"ok": True}
                await run
                required = [e for e in events if e.kind == "interaction.required"]
                resolved = [e for e in events if e.kind == "interaction.resolved"]
                assert len(required) == len(resolved) == (2 if stage == "scope" else 1)
                assert resolved[-1].payload.resolution.decision == (
                    "modified" if stage == "scope" and action == "answered" else "approved" if action == "answered" else "rejected")
                assert resolved[-1].payload.resolution.resolution_reason == {
                    "cancel": "task_cancelled", "timeout": "timed_out", "dismissed": "dismissed", "answered": "answered"}[action]
                assert events[-1].kind == ("turn.cancelled" if action == "cancel" else "turn.completed")
                if action != "answered":
                    from voidx.agent.adapters.persistence.runtime_state_repository import load_runtime_state
                    state = (await load_runtime_state(events[0].session_id)).task_state
                    assert not state.workflow_runs
                    assert state.workflow_route is None
                ids = {e.event_id for e in events}
                assert len(ids) == len(events) and not ids & all_ids
                all_ids.update(ids)
                assert [e.sequence for e in events] == list(range(1, len(events) + 1))
                assert not coordinator._pending and not coordinator._checkpoint_scopes
                assert future.done() and pending.future is None
                assert managed._task.done() and managed._work.done() and managed.channel.completion.done()
                assert bridge.interactions.pending_count == 0
                assert bridge.agent._run is None and bridge.agent._interactions is None
                assert await bridge.session._method_session_respond({**request, "value": "approved"}) == {"ok": False}
                previous = request
            finally:
                if not run.done():
                    await bridge.agent.cancel()
                    await run


@pytest.mark.asyncio
async def test_real_sdk_profile_resolution_failure_cleans_up_and_reuses_session(tmp_path, monkeypatch):
    from voidx.agent.application import agent_profile_snapshot
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock, HeadlessWorkspaceWriteLock
    from voidx.agent.adapters.persistence.session_repository import create_session

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    session = await create_session(workspace=str(tmp_path))
    assert session.profile_snapshot is not None
    original_restore = agent_profile_snapshot.restore_session_profile
    original_close = LangGraphExecution.aclose
    original_init = LangGraphExecution.__init__
    created, closed, snapshots = [], [], []

    def initialize(execution, *args, **kwargs):
        original_init(execution, *args, **kwargs)
        created.append(execution)

    def restore(registry, *, profile_id, snapshot):
        assert profile_id == "coding"
        assert snapshot == session.profile_snapshot
        snapshots.append(snapshot)
        if len(snapshots) == 1:
            raise ValueError("profile resolution failed")
        return original_restore(registry, profile_id=profile_id, snapshot=snapshot)

    async def close(execution):
        await original_close(execution)
        closed.append(execution)

    monkeypatch.setattr(agent_profile_snapshot, "restore_session_profile", restore)
    monkeypatch.setattr(LangGraphExecution, "__init__", initialize)
    monkeypatch.setattr(LangGraphExecution, "aclose", close)
    async with asyncio.timeout(20), VoidxAgent(config, settings=settings) as agent:
        with pytest.raises(ValueError, match="profile resolution failed"):
            async for _ in agent.stream("Write sdk-probe.txt", session_id=session.id, workspace=str(tmp_path)):
                pass
        assert created == closed == []
        assert agent._run is None and agent._interactions is None
        assert agent._initializing is None and not agent._active
        assert not model._histories
        for lock in (HeadlessFileLock("session", session.id), HeadlessWorkspaceWriteLock(str(tmp_path))):
            await lock.acquire()
            lock.release()
        events = [event async for event in agent.stream(
            "Write sdk-probe.txt", session_id=session.id, workspace=str(tmp_path))]
        assert events[-1].kind == "turn.completed"
        assert (tmp_path / "sdk-probe.txt").exists()
    assert len(snapshots) == 3
    assert len(created) == 1
    assert closed == created


@pytest.mark.asyncio
async def test_checkpoint_live_snapshot_failure_cleans_owner(tmp_path, monkeypatch):
    model = CheckpointRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    requests = []

    class Client:
        async def send_text(self, text, **kwargs):
            message = json.loads(text)
            if message.get("method") == "workspace.snapshot":
                nodes = message["params"]["active_snapshot"]["nodes"]
                if any(n["node_type"] == "checkpoint" for n in nodes):
                    raise OSError("checkpoint snapshot unavailable")
            if message.get("method") == "ui.request":
                requests.append(message)
                await bridge.session._method_session_respond({
                    **message["params"], "value": "approved"})

    async with asyncio.timeout(10), bridge:
        await bridge.session.connect(Client())
        with pytest.raises(OSError, match="checkpoint snapshot unavailable"):
            await bridge.run("Checkpoint", workspace=str(tmp_path))
        assert requests == []
        assert bridge.interactions.pending_count == 0
        assert not bridge._active and bridge._run_task is None
        assert bridge.agent._run is None and bridge.agent._interactions is None


@pytest.mark.asyncio
async def test_real_checkpoint_cancel_between_stages_does_not_resolve_unstarted_scope(tmp_path, monkeypatch):
    from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
    from voidx.agent.adapters.persistence.runtime_state_repository import load_runtime_state

    original = InteractionCoordinator.request
    between = asyncio.Event()
    unstarted = []

    async def pause_before_scope(self, request):
        if request.checkpoint_stage == "scope":
            unstarted.append(request.interaction_id)
            between.set()
            await asyncio.Event().wait()
        return await original(self, request)

    monkeypatch.setattr(InteractionCoordinator, "request", pause_before_scope)
    model = CheckpointRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE, lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    requests = asyncio.Queue()
    events = []

    class Client:
        async def send_text(self, text, **kwargs):
            message = json.loads(text)
            if message.get("method") == "ui.request":
                requests.put_nowait(message["params"])

    async with asyncio.timeout(20), bridge:
        await bridge.session.connect(Client())
        run = asyncio.create_task(bridge.run("Checkpoint", workspace=str(tmp_path), observer=events.append))
        try:
            request = await requests.get()
            coordinator = bridge.agent._interactions
            managed = bridge.agent._run
            assert await bridge.session._method_session_respond({**request, "value": "modified"}) == {"ok": True}
            await between.wait()
            assert coordinator._pending == {}
            assert coordinator._checkpoint_scopes == {request["request_id"]}
            assert unstarted[0] not in coordinator._used
            assert await bridge.session._method_session_cancel({"thread_id": request["thread_id"]}) == {"ok": True}
            await run
            required = [e for e in events if e.kind == "interaction.required"]
            resolved = [e for e in events if e.kind == "interaction.resolved"]
            assert len(required) == len(resolved) == 1
            assert required[0].payload.request.checkpoint_stage == "decision"
            assert resolved[0].payload.interaction_id == request["request_id"]
            assert resolved[0].payload.resolution.decision == "modified"
            assert resolved[0].payload.resolution.resolution_reason == "answered"
            assert [e.kind for e in events if e.kind in {"turn.completed", "turn.failed", "turn.cancelled"}] == ["turn.cancelled"]
            assert events[-1].kind == "turn.cancelled"
            assert len({e.event_id for e in events}) == len(events)
            assert [e.sequence for e in events] == list(range(1, len(events) + 1))
            state = (await load_runtime_state(events[0].session_id)).task_state
            assert not state.workflow_runs and state.workflow_route is None
            assert not coordinator._pending and not coordinator._checkpoint_scopes
            assert managed._task.done() and managed._work.done() and managed.channel.completion.done()
            assert bridge.interactions.pending_count == 0
            assert bridge.agent._run is None and bridge.agent._interactions is None
            assert not bridge._active and bridge._run_task is None
            assert requests.empty()
            assert await bridge.session._method_session_respond({**request, "value": "approved"}) == {"ok": False}
        finally:
            if not run.done():
                await bridge.agent.cancel()
                await run
