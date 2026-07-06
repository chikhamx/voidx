import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest
import websockets


from voidx.ui.output.dock import BottomInputDock
from voidx.ui.output.events.schema import AssistantStreamUpdated, RefreshRequested, TurnStarted
from voidx.ui.output.events import CompositeEventConsumer, DockEventConsumer
from voidx.ui.gateway.session import GatewayEventConsumer, GatewaySession
from voidx.ui.gateway.server import GatewayServer
from voidx.ui.protocol.requests import UiChoiceRequest, UiResponse, UiTextRequest


class FakeClient:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.messages: list[str] = []
        self.fail_after = fail_after

    async def send_text(self, text: str) -> None:
        if self.fail_after is not None and len(self.messages) >= self.fail_after:
            raise RuntimeError("client disconnected")
        self.messages.append(text)


def _payloads(client: FakeClient) -> list[dict]:
    return [json.loads(message) for message in client.messages]


def _method(msg: dict) -> str:
    return msg["method"]


def _params(msg: dict) -> dict:
    return msg["params"]


async def _wait_for_log_record(caplog, message: str):
    for _ in range(10):
        for record in caplog.records:
            if record.getMessage() == message:
                return record
        await asyncio.sleep(0)
    return None


@pytest.mark.asyncio
async def test_gateway_connect_sends_snapshot_from_current_tree():
    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("hello")
    session = GatewaySession(lambda: dock.tree, thread_id="session_1")
    client = FakeClient()

    await session.connect(client)

    msg = json.loads(client.messages[0])

    assert msg["method"] == "workspace.snapshot"
    assert msg["params"]["active_thread_id"] == "session_1"
    assert msg["params"]["active_snapshot"]["nodes"][0]["header"].endswith("hello")


@pytest.mark.asyncio
async def test_gateway_snapshot_includes_runtime_and_write_lock_status():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    await session.register_thread("t2")
    session._run_manager.mark_running("t1")
    await session._run_manager.acquire_workspace_write_lock("t1")
    waiting = asyncio.create_task(session._run_manager.acquire_workspace_write_lock("t2"))
    await asyncio.sleep(0)
    client = FakeClient()

    try:
        await session.connect(client)

        msg = json.loads(client.messages[0])
        runtime = msg["params"]["runtime"]
        write_lock = msg["params"]["workspace_write_lock"]
        threads = {thread["thread_id"]: thread for thread in msg["params"]["threads"]}

        assert runtime["active_thread_ids"] == ["t1", "t2"]
        assert runtime["max_concurrent_sessions"] == 2
        assert write_lock == {"holder_thread_id": "t1", "waiting_thread_ids": ["t2"]}
        assert threads["t1"]["status"] == "running"
        assert threads["t2"]["status"] == "waiting_for_write_lock"
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_gateway_broadcasts_events_with_incrementing_sequences():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)

    await session.broadcast_event(AssistantStreamUpdated(text="hello"))

    snapshot = json.loads(client.messages[0])
    event = json.loads(client.messages[1])

    assert snapshot["method"] == "workspace.snapshot"
    assert event["method"] in ("item.started", "item.delta")
    assert event["params"]["kind"] == "assistant_stream"


@pytest.mark.asyncio
async def test_gateway_consumer_rebroadcasts_snapshot_on_refresh():
    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("hello")
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    consumer = GatewayEventConsumer(session)

    await consumer.handle(RefreshRequested())

    messages = [json.loads(message) for message in client.messages]
    assert messages[-1]["method"] == "workspace.snapshot"
    assert messages[-2]["method"] == "refresh.requested"


@pytest.mark.asyncio
async def test_gateway_broadcast_snapshot_updates_clients():
    dock = BottomInputDock()
    dock.begin_capture()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    dock.start_turn("after snapshot")

    await session.broadcast_snapshot()

    snapshot = json.loads(client.messages[-1])
    assert snapshot["method"] == "workspace.snapshot"
    assert snapshot["params"]["active_snapshot"]["nodes"][-1]["header"].endswith("after snapshot")


def test_emit_web_gateway_bootstrap_writes_marker(capsys):
    from voidx.ui.gateway.bootstrap import emit_web_gateway_bootstrap

    emit_web_gateway_bootstrap("ws://127.0.0.1:8787/?token=abc")

    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err.strip().removeprefix("VOIDX_WEB_GATEWAY"))
    assert payload == {
        "type": "web_gateway",
        "url": "ws://127.0.0.1:8787/?token=abc",
        "token": "abc",
    }


@pytest.mark.asyncio
async def test_gateway_removes_clients_that_fail_during_broadcast():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    failing = FakeClient(fail_after=1)
    healthy = FakeClient()
    await session.connect(failing)
    await session.connect(healthy)

    await session.broadcast_event(AssistantStreamUpdated(text="first"))
    await session.broadcast_event(AssistantStreamUpdated(text="second"))

    assert failing not in session.clients
    item_methods = [
        msg["method"] for msg in _payloads(healthy)
        if msg.get("method") in ("item.started", "item.delta")
    ]
    assert len(item_methods) == 2


@pytest.mark.asyncio
async def test_composite_event_consumer_keeps_dock_primary_and_mirrors_events():
    dock = BottomInputDock()
    dock.begin_capture()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)
    consumer = CompositeEventConsumer(
        primary=DockEventConsumer(dock),
        mirrors=[GatewayEventConsumer(session)],
    )

    result = consumer.handle(TurnStarted(text="demo"))
    if asyncio.iscoroutine(result):
        result = await result
    await asyncio.sleep(0)

    assert result is dock.current_turn
    assert dock.current_turn is not None
    messages = _payloads(client)
    assert any(msg.get("method") == "turn.started" and msg["params"].get("text") == "demo" for msg in messages)


@pytest.mark.asyncio
async def test_composite_event_consumer_handle_direct_logs_async_mirror_error(caplog):
    class FailingMirror:
        async def handle(self, _event):
            await asyncio.sleep(0)
            raise RuntimeError("mirror failed")

    dock = BottomInputDock()
    dock.begin_capture()
    consumer = CompositeEventConsumer(
        primary=DockEventConsumer(dock),
        mirrors=[FailingMirror()],
    )
    caplog.set_level(logging.WARNING, logger="voidx.ui.output.events")

    result = consumer.handle_direct(TurnStarted(text="demo"))
    record = await _wait_for_log_record(
        caplog,
        "UI event direct mirror consumer failed",
    )

    assert result is dock.current_turn
    assert record is not None
    assert isinstance(record.exc_info[1], RuntimeError)
    assert str(record.exc_info[1]) == "mirror failed"


@pytest.mark.asyncio
async def test_composite_event_consumer_handle_direct_logs_async_primary_error(caplog):
    class FailingPrimary:
        async def handle(self, _event):
            await asyncio.sleep(0)
            raise RuntimeError("primary failed")

    consumer = CompositeEventConsumer(primary=FailingPrimary())
    caplog.set_level(logging.WARNING, logger="voidx.ui.output.events")

    consumer.handle_direct(TurnStarted(text="demo"))
    record = await _wait_for_log_record(
        caplog,
        "UI event direct primary consumer failed",
    )

    assert record is not None
    assert isinstance(record.exc_info[1], RuntimeError)
    assert str(record.exc_info[1]) == "primary failed"


@pytest.mark.asyncio
async def test_websocket_gateway_sends_snapshot_and_broadcast_event():
    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("hello")
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    server = GatewayServer(session, host="127.0.0.1", port=0, token="abc")
    await server.start()
    try:
        async with websockets.connect(server.url) as websocket:
            snapshot = json.loads(await websocket.recv())
            assert snapshot["method"] == "workspace.snapshot"

            await session.broadcast_event(AssistantStreamUpdated(text="hi web"))
            event = json.loads(await websocket.recv())

            assert event["method"] in ("item.started", "item.delta")
            assert event["params"]["kind"] == "assistant_stream"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_websocket_gateway_dispatches_submit_commands():
    dock = BottomInputDock()
    received: list[str] = []

    async def handle_submit(params):
        received.append(params["text"])
        return {"ok": True}

    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    session.methods.register("session.submit", handle_submit)
    server = GatewayServer(session, host="127.0.0.1", port=0, token="abc")
    await server.start()
    try:
        async with websockets.connect(server.url) as websocket:
            await websocket.recv()  # snapshot
            await websocket.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.submit",
                "params": {"text": "from web"},
            }))
            response = json.loads(await websocket.recv())
            assert response["result"] == {"ok": True}

        assert received == ["from web"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_gateway_request_sends_request_and_resolves_response():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)

    task = asyncio.create_task(session.request(UiChoiceRequest(
        request_id="req_1",
        prompt="Mode",
        choices=[("Auto", "auto", "")],
    )))
    for _ in range(20):
        if len(client.messages) > 1:
            break
        await asyncio.sleep(0.01)
    request = json.loads(client.messages[-1])

    assert request["method"] == "ui.request"
    assert request["params"]["request_id"] == "req_1"
    assert request["params"]["thread_id"] == "t1"

    await session.handle_response(UiResponse(request_id="req_1", value="auto"))

    assert await task == UiResponse(request_id="req_1", value="auto")


@pytest.mark.asyncio
async def test_gateway_handles_consecutive_choice_then_text_requests():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client)

    choice_task = asyncio.create_task(session.request(UiChoiceRequest(
        request_id="choice_1",
        prompt="Mode",
        choices=[("Other", "other", "")],
    )))
    for _ in range(20):
        if len(client.messages) > 1:
            break
        await asyncio.sleep(0.01)
    choice_request = json.loads(client.messages[-1])
    assert choice_request["method"] == "ui.request"
    assert choice_request["params"]["kind"] == "choice"
    await session.handle_response(UiResponse(request_id="choice_1", value="other"))
    assert await choice_task == UiResponse(request_id="choice_1", value="other")

    text_task = asyncio.create_task(session.request(UiTextRequest(
        request_id="text_1",
        prompt="Custom answer",
    )))
    for _ in range(20):
        if len(client.messages) > 2:
            break
        await asyncio.sleep(0.01)
    text_request = json.loads(client.messages[-1])
    assert text_request["method"] == "ui.request"
    assert text_request["params"]["kind"] == "text"
    await session.handle_response(UiResponse(request_id="text_1", value="custom answer"))
    assert await text_task == UiResponse(request_id="text_1", value="custom answer")


@pytest.mark.asyncio
async def test_websocket_gateway_dispatches_responses_to_pending_request():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    server = GatewayServer(session, host="127.0.0.1", port=0, token="abc")
    await server.start()
    try:
        async with websockets.connect(server.url) as websocket:
            await websocket.recv()  # snapshot
            task = asyncio.create_task(session.request(UiChoiceRequest(
                request_id="req_ws",
                prompt="Mode",
                choices=[("Auto", "auto", "")],
            )))
            request = json.loads(await websocket.recv())
            assert request["method"] == "ui.request"
            await websocket.send(json.dumps({
                "jsonrpc": "2.0",
                "id": "req_ws",
                "result": {"value": "auto"},
            }))

            assert await asyncio.wait_for(task, timeout=1) == UiResponse(
                request_id="req_ws",
                value="auto",
            )
    finally:
        await server.stop()
