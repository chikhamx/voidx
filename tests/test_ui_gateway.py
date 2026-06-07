import asyncio
import json
import sys
from pathlib import Path

import pytest
import websockets

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.ui.output.dock import BottomInputDock
from voidx.ui.output.events.schema import AssistantStreamUpdated, RefreshRequested, TurnStarted
from voidx.ui.output.events import CompositeEventConsumer, DockEventConsumer
from voidx.ui.gateway.session import GatewayEventConsumer, GatewaySession
from voidx.ui.gateway.server import GatewayServer
from voidx.ui.protocol import (
    UiChoiceRequest,
    UiCommandEnvelope,
    UiResponse,
    UiResponseEnvelope,
    UiSubmitCommand,
    UiTextRequest,
    parse_protocol_envelope,
)


class FakeClient:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.messages: list[str] = []
        self.fail_after = fail_after

    async def send_text(self, text: str) -> None:
        if self.fail_after is not None and len(self.messages) >= self.fail_after:
            raise RuntimeError("client disconnected")
        self.messages.append(text)


def _payloads(client: FakeClient) -> list[object]:
    return [
        parse_protocol_envelope(json.loads(message)).payload
        for message in client.messages
    ]


@pytest.mark.asyncio
async def test_gateway_connect_sends_snapshot_from_current_tree():
    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("hello")
    session = GatewaySession(lambda: dock.tree, session_id="session_1")
    client = FakeClient()

    await session.connect(client)

    envelope = parse_protocol_envelope(json.loads(client.messages[0]))

    assert envelope.type == "snapshot"
    assert envelope.payload.session_id == "session_1"
    assert envelope.payload.nodes[0].header.endswith("hello")


@pytest.mark.asyncio
async def test_gateway_broadcasts_events_with_incrementing_sequences():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree)
    client = FakeClient()
    await session.connect(client)

    await session.broadcast_event(AssistantStreamUpdated(text="hello"))

    snapshot = parse_protocol_envelope(json.loads(client.messages[0]))
    event = parse_protocol_envelope(json.loads(client.messages[1]))

    assert snapshot.seq == 1
    assert event.seq == 2
    assert event.type == "event"
    assert event.payload.text == "hello"


@pytest.mark.asyncio
async def test_gateway_consumer_rebroadcasts_snapshot_on_refresh():
    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("hello")
    session = GatewaySession(lambda: dock.tree)
    client = FakeClient()
    await session.connect(client)
    consumer = GatewayEventConsumer(session)

    await consumer.handle(RefreshRequested())

    envelopes = [parse_protocol_envelope(json.loads(message)) for message in client.messages]
    assert envelopes[-1].type == "snapshot"
    assert envelopes[-2].type == "event"
    assert envelopes[-2].payload.kind == "refresh.requested"


@pytest.mark.asyncio
async def test_gateway_broadcast_snapshot_updates_clients():
    dock = BottomInputDock()
    dock.begin_capture()
    session = GatewaySession(lambda: dock.tree)
    client = FakeClient()
    await session.connect(client)
    dock.start_turn("after snapshot")

    await session.broadcast_snapshot()

    snapshot = parse_protocol_envelope(json.loads(client.messages[-1]))
    assert snapshot.type == "snapshot"
    assert snapshot.payload.nodes[-1].header.endswith("after snapshot")


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
    session = GatewaySession(lambda: dock.tree)
    failing = FakeClient(fail_after=1)
    healthy = FakeClient()
    await session.connect(failing)
    await session.connect(healthy)

    await session.broadcast_event(AssistantStreamUpdated(text="first"))
    await session.broadcast_event(AssistantStreamUpdated(text="second"))

    assert failing not in session.clients
    assert [payload.text for payload in _payloads(healthy) if hasattr(payload, "text")] == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_composite_event_consumer_keeps_dock_primary_and_mirrors_events():
    dock = BottomInputDock()
    dock.begin_capture()
    session = GatewaySession(lambda: dock.tree)
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
    payloads = _payloads(client)
    assert any(isinstance(payload, TurnStarted) and payload.text == "demo" for payload in payloads)


@pytest.mark.asyncio
async def test_websocket_gateway_sends_snapshot_and_broadcast_event():
    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("hello")
    session = GatewaySession(lambda: dock.tree)
    server = GatewayServer(session, host="127.0.0.1", port=0, token="secret")
    await server.start()
    try:
        async with websockets.connect(server.url) as websocket:
            snapshot = parse_protocol_envelope(json.loads(await websocket.recv()))
            assert snapshot.type == "snapshot"

            await session.broadcast_event(AssistantStreamUpdated(text="hi web"))
            event = parse_protocol_envelope(json.loads(await websocket.recv()))

            assert event.type == "event"
            assert event.payload.text == "hi web"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_websocket_gateway_dispatches_submit_commands():
    dock = BottomInputDock()
    received: list[str] = []

    async def handle_command(command):
        received.append(command.text)

    session = GatewaySession(lambda: dock.tree, command_handler=handle_command)
    server = GatewayServer(session, host="127.0.0.1", port=0, token="")
    await server.start()
    try:
        async with websockets.connect(server.url) as websocket:
            await websocket.recv()
            await websocket.send(
                UiCommandEnvelope(payload=UiSubmitCommand(text="from web")).model_dump_json()
            )
            for _ in range(20):
                if received:
                    break
                await asyncio.sleep(0.01)

        assert received == ["from web"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_gateway_request_sends_request_and_resolves_response():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree)
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
    request = parse_protocol_envelope(json.loads(client.messages[-1]))

    assert request.type == "request"
    assert request.payload.request_id == "req_1"

    await session.handle_response(UiResponse(request_id="req_1", value="auto"))

    assert await task == UiResponse(request_id="req_1", value="auto")


@pytest.mark.asyncio
async def test_gateway_handles_consecutive_choice_then_text_requests():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree)
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
    choice_request = parse_protocol_envelope(json.loads(client.messages[-1]))
    assert choice_request.type == "request"
    assert choice_request.payload.kind == "choice"
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
    text_request = parse_protocol_envelope(json.loads(client.messages[-1]))
    assert text_request.type == "request"
    assert text_request.payload.kind == "text"
    await session.handle_response(UiResponse(request_id="text_1", value="custom answer"))
    assert await text_task == UiResponse(request_id="text_1", value="custom answer")


@pytest.mark.asyncio
async def test_websocket_gateway_dispatches_responses_to_pending_request():
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree)
    server = GatewayServer(session, host="127.0.0.1", port=0, token="")
    await server.start()
    try:
        async with websockets.connect(server.url) as websocket:
            await websocket.recv()
            task = asyncio.create_task(session.request(UiChoiceRequest(
                request_id="req_ws",
                prompt="Mode",
                choices=[("Auto", "auto", "")],
            )))
            request = parse_protocol_envelope(json.loads(await websocket.recv()))
            assert request.type == "request"
            await websocket.send(UiResponseEnvelope(
                payload=UiResponse(request_id="req_ws", value="auto")
            ).model_dump_json())

            assert await asyncio.wait_for(task, timeout=1) == UiResponse(
                request_id="req_ws",
                value="auto",
            )
    finally:
        await server.stop()
