from uuid import uuid4
import pytest

from voidx.presentation.gateway.semantic_interactions import SemanticInteractionRouter
from voidx.presentation.gateway.session.core import GatewaySession
from voidx.presentation.output.tree import OutputTree
from voidx.presentation.protocol.requests import UiPermissionRequest
from voidx.tooling.domain.interaction import InteractionRequest
from voidx.tooling.domain.ui_events import ChoicePayload


def request(iid="permission"):
    return InteractionRequest(interaction_id=iid, session_id="s", thread_id="t", turn_id="turn",
        input_kind="permission", purpose="permission", prompt="Allow?",
        choices=[ChoicePayload(label=v, value=v) for v in ("allow", "session", "deny")])


@pytest.mark.asyncio
async def test_route_strict_ownership_duplicates_and_resolution():
    answers = []
    async def submit(iid, answer):
        answers.append((iid, answer))
        return True
    route = SemanticInteractionRouter(submit)
    session = GatewaySession(OutputTree, interaction_router=route)
    r = request()
    route.register(r, UiPermissionRequest(request_id=r.interaction_id, thread_id="t", prompt="Allow?"))
    assert await session._method_session_respond(dict(request_id="permission", value="allow", thread_id="wrong")) == {"ok": False}
    assert not answers
    assert await session._method_session_respond(dict(request_id="permission", value="allow", thread_id="t")) == {"ok": True}
    assert answers[0][1].session_id == "s"
    assert answers[0][1].turn_id == "turn"
    assert answers[0][1].scope is None
    assert await session._method_session_respond(dict(request_id="permission", value="deny", thread_id="t")) == {"ok": False}
    route.resolve("permission", session_id="s", thread_id="t", turn_id="turn")
    assert route.pending_count == 0
    assert await session._method_session_respond(dict(request_id="permission", value="allow", thread_id="t")) == {"ok": False}


@pytest.mark.asyncio
async def test_bounded_pending_and_exact_options():
    async def submit(iid, answer):
        return True
    route = SemanticInteractionRouter(submit)
    for i in range(64):
        r = request(str(i))
        route.register(r, UiPermissionRequest(request_id=str(i), thread_id="t", prompt="Allow?"))
    with pytest.raises(ValueError, match="64"):
        route.register(request("overflow"), UiPermissionRequest(request_id="overflow", thread_id="t", prompt="Allow?"))
    assert not await route.respond("0", "yes", thread_id="t")
    assert await route.respond("0", None, thread_id="t")
    route.close()
    assert route.pending_count == 0


@pytest.mark.asyncio
async def test_no_client_reconnect_and_send_failure():
    import json
    async def submit(iid, answer):
        return True
    route = SemanticInteractionRouter(submit)
    session = GatewaySession(OutputTree, interaction_router=route)
    r = request()
    ui = UiPermissionRequest(request_id=r.interaction_id, thread_id="t", prompt="Allow?")
    route.register(r, ui)
    await session.publish_interaction(ui)
    assert route.pending_count == 1
    class Client:
        def __init__(self):
            self.messages = []
        async def send_text(self, text, **kwargs):
            self.messages.append(json.loads(text))
    client = Client()
    await session.connect(client)
    assert client.messages[-1]["method"] == "ui.request"
    session.disconnect(client)
    assert route.pending_count == 1
    second = Client()
    await session.connect(second)
    assert second.messages[-1]["params"]["request_id"] == "permission"
    class Broken:
        async def send_text(self, text, **kwargs):
            raise OSError("send failed")
    session.disconnect(second)
    session._clients.add(Broken())
    with pytest.raises(OSError, match="send failed"):
        await session.publish_interaction(ui)
    assert route.pending_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", [False, True])
async def test_semantic_event_send_failure_is_not_swallowed(encoded):
    async def submit(iid, response):
        return True
    route = SemanticInteractionRouter(submit)
    session = GatewaySession(OutputTree, interaction_router=route)
    class Broken:
        async def send_text(self, text, **kwargs):
            raise OSError("event send failed")
    client = Broken()
    session._clients.add(client)
    with pytest.raises(OSError, match="event send failed"):
        if encoded:
            await session._send_encoded([(client, "{}")])
        else:
            await session._broadcast("{}")
    with pytest.raises(RuntimeError, match="closed"):
        route.register(request(), UiPermissionRequest(request_id="permission", thread_id="t", prompt="?"))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_fails", [False, True])
async def test_reconnect_pending_send_failure_cancels_owner(cancel_fails):
    import json
    cancelled = []

    async def cancel():
        cancelled.append(True)
        if cancel_fails:
            raise RuntimeError("cancel failed")

    async def submit(*args):
        return True

    route = SemanticInteractionRouter(submit)
    session = GatewaySession(OutputTree, interaction_router=route, interaction_cancel=cancel)
    route.register(request(), UiPermissionRequest(request_id="permission", thread_id="t", prompt="?"))

    class Client:
        async def send_text(self, text, **kwargs):
            if json.loads(text).get("method") == "ui.request":
                raise OSError("reconnect failed")

    client = Client()
    with pytest.raises(BaseException) as caught:
        await session.connect(client)
    assert cancelled == [True]
    assert route.pending_count == 0
    assert client not in session._clients
    if cancel_fails:
        assert isinstance(caught.value, ExceptionGroup)
        assert [str(e) for e in caught.value.exceptions] == ["reconnect failed", "cancel failed"]
    else:
        assert isinstance(caught.value, OSError)


@pytest.mark.asyncio
async def test_submitted_unresolved_reconnect_and_full_channel_cancel(monkeypatch):
    import asyncio
    import json
    from voidx.agent.application.runtime.semantic_channel import SemanticChannel
    from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
    from voidx.agent.domain import semantic_events as e

    identity = dict(session_id="s", thread_id="t", turn_id="turn")
    channel = SemanticChannel(**identity, capacity=1)
    coordinator = InteractionCoordinator(channel, **identity)
    route = SemanticInteractionRouter(coordinator.submit_interaction)
    session = GatewaySession(OutputTree, interaction_router=route)
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(coordinator.request(request()))
    try:
        await asyncio.sleep(0)
        assert channel._queue.full()
        route.register(request(), UiPermissionRequest(request_id="permission", thread_id="t", prompt="?"))
        assert await route.respond("permission", "allow", thread_id="t")
        assert not await route.respond("permission", "deny", thread_id="t")
        def forbidden(*args, **kwargs):
            raise AssertionError("no task-per-put")
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_task", forbidden)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert not task.done(), "resolved publication must be blocked by the full channel"
        assert asyncio.all_tasks() == baseline | {task}
        messages = []
        class Client:
            async def send_text(self, text, **kwargs):
                messages.append(json.loads(text))
        client = Client()
        await session.connect(client)
        session.disconnect(client)
        if session._persisted_sync_task is not None:
            await session._persisted_sync_task
        assert not any(m.get("method") == "ui.request" for m in messages)
        assert route.pending_count == 1
        assert not session._run_manager.actor("t").state.pending_requests
        coordinator.begin_cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        tail = await coordinator.cleanup("cancelled")
        assert len(tail) == 1
        channel._stop_publishing()
        channel._finish(e.TurnCancelled(**identity, event_id=uuid4(), sequence=1, agent_id=None, parent_tool_call_id=None, timestamp=0.0, payload={"reason": "user"}), tail)
        events = [event async for event in channel.events()]
        assert [event.kind for event in events] == ["interaction.required", "interaction.resolved", "turn.cancelled"]
        assert asyncio.all_tasks() == baseline
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_closed_display_accepts_cleanup_resolution_without_reopening():
    route = SemanticInteractionRouter(None)
    route.register(request(), UiPermissionRequest(request_id="permission", thread_id="t", prompt="?"))
    route.close()
    route.resolve("permission", session_id="s", thread_id="t", turn_id="turn")
    assert route.requests() == ()
    assert route.pending_count == 0
    with pytest.raises(RuntimeError, match="closed"):
        route.register(request(), UiPermissionRequest(request_id="permission", thread_id="t", prompt="?"))


@pytest.mark.asyncio
@pytest.mark.parametrize("value,free_text", [("Staging", False), ("free:literal", True), ("", True), (None, False)])
async def test_clarify_raw_strings_and_ownership(value, free_text):
    from voidx.presentation.protocol.requests import UiChoiceRequest
    answers = []

    async def submit(iid, answer):
        answers.append(answer)
        return True

    route = SemanticInteractionRouter(submit)
    r = request("clarify").model_copy(update={"purpose": "clarify", "input_kind": "choice",
        "allow_free_text": True, "choices": [ChoicePayload(label="Staging", value="Staging")]})
    route.register(r, UiChoiceRequest(request_id="clarify", thread_id="t", prompt="?"))
    assert not await route.respond("clarify", value, thread_id="wrong")
    assert not await route.respond("clarify", {"value": "Staging"}, thread_id="t")
    assert await route.respond("clarify", value, thread_id="t")
    assert answers[0].value == (value or "")
    assert answers[0].free_text is free_text
    assert answers[0].cancelled is (value is None)
    assert (answers[0].session_id, answers[0].thread_id, answers[0].turn_id) == ("s", "t", "turn")
    assert not await route.respond("clarify", value, thread_id="t")
    with pytest.raises(ValueError, match="ownership"):
        route.resolve("clarify", session_id="s", thread_id="t", turn_id="old")
    route.resolve("clarify", session_id="s", thread_id="t", turn_id="turn")
    assert not await route.respond("clarify", value, thread_id="t")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,value,free_text", [("decision", "approved", False),
    ("decision", "needs_doc", False), ("decision", "modified", False),
    ("decision", "rejected", False), ("decision", "custom", True),
    ("scope", "approved", True), ("scope", "no", True), ("scope", None, False)])
async def test_checkpoint_router_exact_values_and_stale(stage, value, free_text):
    from voidx.presentation.protocol.requests import UiChoiceRequest, UiTextRequest
    answers = []

    async def submit(iid, response):
        answers.append(response)
        return True

    route = SemanticInteractionRouter(submit)
    iid = "card" if stage == "decision" else "scope"
    r = InteractionRequest(interaction_id=iid, session_id="s", thread_id="t", turn_id="turn",
        purpose="checkpoint", input_kind="choice" if stage == "decision" else "text",
        checkpoint_id="card", checkpoint_stage=stage, checkpoint={"goal": "Ship"},
        prompt="Plan", allow_free_text=True, choices=[ChoicePayload(label=v, value=v)
            for v in ("approved", "needs_doc", "modified", "rejected")] if stage == "decision" else [])
    cls = UiChoiceRequest if stage == "decision" else UiTextRequest
    route.register(r, cls(request_id=iid, thread_id="t", prompt="Plan"))
    with pytest.raises(ValueError, match="Duplicate"):
        route.register(r, cls(request_id=iid, thread_id="t", prompt="Plan"))
    assert not await route.respond(iid, value, thread_id="wrong")
    assert not await route.respond(iid, 123, thread_id="t")
    assert not answers
    assert await route.respond(iid, value, thread_id="t")
    assert answers[0].free_text == free_text
    assert answers[0].cancelled == (value is None)
    assert not await route.respond(iid, value, thread_id="t")
    route.resolve(iid, session_id="s", thread_id="t", turn_id="turn")
    assert not await route.respond(iid, value, thread_id="t")
    assert route.pending_count == 0
