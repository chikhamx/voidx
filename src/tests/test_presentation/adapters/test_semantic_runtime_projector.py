"""Runtime compatibility through the opt-in projector and real consumers."""
import inspect
import json

import pytest

from voidx.agent.domain import semantic_events as s
from voidx.presentation.adapters.semantic_event_projector import SemanticEventProjector
from voidx.presentation.gateway.session import GatewaySession
from voidx.presentation.output.events.consumers import DockEventConsumer
from tests.test_presentation.adapters.test_semantic_event_projector import Client, event, start
from tests.test_presentation.gateway.conftest import configured_settings_factory, isolated_dock, _plain


def pressure(seq, *, level="soft", outcome="hint_injected", pressure_id="p", **scope):
    return event(s.ContextPressureUpdated, seq, **scope, pressure_id=pressure_id,
                 level=level, action="converge_hint", outcome=outcome,
                 reason="threshold", can_compact=True, turn_count=3, pre_tokens=81,
                 soft_threshold=75, hard_threshold=90)


def finish(seq, *, level="soft", pressure_id="p", **scope):
    return event(s.ContextPressureFinished, seq, **scope, pressure_id=pressure_id,
                 level=level, outcome="compacted", detail="done", ok=False)


@pytest.mark.parametrize("cls", [s.DiagnosticWarning, s.DiagnosticError])
def test_diagnostic_public_message_and_detached_typed_metadata(cls):
    p = SemanticEventProjector()
    start(p)
    source = event(cls, 2, code="INTERNAL", summary="Public message", recoverable=True)
    with pytest.raises(ValueError, match="project_result"):
        p.project(source)
    result = p.project_result(source)
    assert result.events[0].message == "Public message"
    assert result.runtime == source and type(result.runtime) is cls
    assert result.runtime is not source and result.runtime.payload is not source.payload
    assert result.runtime.model_dump() == source.model_dump()


@pytest.mark.parametrize("source", ["user", "guard", "system"])
def test_guidance_batches_no_one_to_one_constraint(source):
    p = SemanticEventProjector()
    start(p)
    for seq in (2, 3):
        e = event(s.GuidanceSubmitted, seq, text="same", source=source, truncated=True)
        with pytest.raises(ValueError, match="project_result"):
            p.project(e)
        result = p.project_result(e)
        assert result.events[0].text == "same" and result.events[0].truncated
        assert result.runtime == e and result.runtime is not e
    for seq in (4, 5):
        e = event(s.GuidanceCommitted, seq, text="same\nsame", source=source, truncated=True)
        result = p.project_result(e)
        assert result.events[0].source == source and result.events[0].truncated
    e = event(s.GuidanceApplied, 6, text="same\nsame", source=source, truncated=True)
    assert p.project_result(e).events == ()
    e = event(s.GuidanceCommitted, 7, text="", source="system", truncated=False)
    assert p.project_result(e).events[0].text == ""


@pytest.mark.parametrize("cls", [s.GuidanceCommitted, s.GuidanceApplied])
def test_guidance_without_preview_is_legal_and_requires_result(cls):
    p = SemanticEventProjector()
    start(p)
    e = event(cls, 2, text="", source="system", truncated=False)
    with pytest.raises(ValueError, match="project_result"):
        p.project(e)
    result = p.project_result(e)
    assert result.runtime == e and result.runtime.payload is not e.payload
    assert len(result.events) == (cls is s.GuidanceCommitted)


def test_pressure_full_fields_lifecycle_and_retry():
    p = SemanticEventProjector()
    start(p)
    for e in (finish(2), pressure(2, outcome="hint_present"),
              pressure(2, level="hard", outcome="hint_upgraded")):
        with pytest.raises(ValueError):
            p.project_result(e)
    e = pressure(2)
    with pytest.raises(ValueError, match="project_result"):
        p.project(e)
    result = p.project_result(e)
    assert result.events[0].model_dump(include=set(type(e.payload).model_fields)) == e.payload.model_dump()
    assert result.runtime == e and result.runtime is not e
    for e in (pressure(3), pressure(3, level="hard", outcome="hint_present"),
              pressure(3, outcome="hint_upgraded"), finish(3, level="hard"),
              pressure(4, outcome="hint_present")):
        with pytest.raises(ValueError):
            p.project_result(e)
    p.project_result(pressure(3, outcome="hint_present"))
    p.project_result(pressure(4, level="hard", outcome="hint_upgraded"))
    # The old hint updater keeps a hard hint when a soft decision says present.
    p.project_result(pressure(5, level="soft", outcome="hint_present"))
    with pytest.raises(ValueError):
        p.project_result(finish(6))
    e = finish(6, level="hard")
    result = p.project_result(e)
    assert result.events[0].model_dump(include=set(type(e.payload).model_fields)) == e.payload.model_dump()
    assert result.runtime == e and result.runtime.payload is not e.payload
    for e in (finish(7, level="hard"), pressure(7),
              pressure(7, level="hard", outcome="hint_upgraded")):
        with pytest.raises(ValueError):
            p.project_result(e)
    p.project_result(pressure(7, pressure_id="new", level="hard"))


@pytest.mark.parametrize("terminal,payload", [
    (s.TurnCancelled, {"reason": "cancelled"}),
    (s.TurnFailed, {"code": "failed", "summary": "failed", "recoverable": False}),
    (s.TurnCompleted, {"usage": {}}),
])
def test_runtime_terminal_cleans_without_inventing_finish(terminal, payload):
    p = SemanticEventProjector()
    start(p)
    p.project_result(pressure(2))
    p.project_result(event(s.GuidanceSubmitted, 3, text="pending", source="user", truncated=False))
    result = p.project_result(event(terminal, 4, **payload))
    assert len(result.events) == 1
    assert not any(e.kind.startswith(("context_pressure", "guidance")) for e in result.events)
    assert p.active_turn_count == 0
    start(p)
    p.project_result(pressure(2))


@pytest.mark.parametrize("scope", [{"session_id": "other"}, {"thread_id": "other"}, {"turn_id": "other"}])
def test_runtime_state_isolated_by_full_scope(scope):
    p = SemanticEventProjector()
    start(p)
    begun = event(s.TurnStarted, 1, text="other", metadata={}).model_copy(update=scope)
    p.project(begun)
    p.project_result(pressure(2))
    p.project_result(pressure(2, level="hard").model_copy(update=scope))
    p.project_result(finish(3))
    p.project_result(finish(3, level="hard").model_copy(update=scope))


@pytest.mark.asyncio
async def test_real_dock_gateway_runtime_projection(isolated_dock, configured_settings_factory):
    p = SemanticEventProjector()
    dock = isolated_dock
    dock.begin_capture()
    consumer = DockEventConsumer(dock)
    gateway = GatewaySession(lambda: dock.tree, thread_id="t")
    client = Client()
    await gateway.connect(client)

    async def consume(e):
        result = p.project_result(e)
        for old in result.events:
            pending = consumer.handle(old)
            if inspect.isawaitable(pending):
                await pending
            await gateway.broadcast_event(old)
        return result

    await consume(event(s.TurnStarted, 1, text="initial", metadata={}))
    for seq, cls in enumerate((s.DiagnosticWarning, s.DiagnosticError), 2):
        await consume(event(cls, seq, code="PRIVATE_CODE", summary=f"public-{seq}", recoverable=False))
    rendered = "\n".join(_plain(line) for line in dock.tree.render(100))
    wire = json.dumps(client.messages)
    for text in ("public-2", "public-3"):
        assert text in rendered and text in wire
    assert "PRIVATE_CODE" not in rendered + wire and "recoverable" not in rendered + wire
    await consume(pressure(4))
    assert dock.status_record("p").detail == "threshold"
    await consume(pressure(5, level="hard", outcome="hint_upgraded"))
    assert "hard" in dock.status_record("p").label
    await consume(finish(6, level="hard"))
    assert dock.status_record("p") is None
    for seq, text in ((7, "first"), (8, "second")):
        await consume(event(s.GuidanceSubmitted, seq, text=text, source="user", truncated=True))
        assert dock._guidance_preview == text
    await consume(event(s.GuidanceCommitted, 9, text="first\nsecond", source="user", truncated=True))
    assert not dock._guidance_preview
    before_tree = repr(dock.tree.root.children)
    before_messages = list(client.messages)
    result = await consume(event(s.GuidanceApplied, 10, text="first\nsecond", source="user", truncated=True))
    assert result.events == () and result.runtime.kind == "guidance.applied"
    assert client.messages == before_messages
    assert repr(dock.tree.root.children) == before_tree
    turns = [n for n in dock.tree.root.children if n.node_type == "turn" and n.payload.get("style") == "guidance"]
    assert len(turns) == 1 and turns[0].payload["raw_text"] == "first\nsecond"
    items = [m["params"] for m in client.messages if m.get("method", "").startswith("item.")]
    previews = [i for i in items if i.get("kind") == "guidance_preview"]
    assert len(previews) == 3
    assert previews[0]["data"] == {"text": "first", "truncated": True}
    assert previews[-1]["data"] == {}
    statuses = [i for i in items if i.get("kind") == "status" and i.get("data", {}).get("pressure_id") == "p"]
    assert len(statuses) == 3
    assert len({i["item_id"] for i in statuses}) == 1
    for item, e in zip(statuses, (pressure(4), pressure(5, level="hard", outcome="hint_upgraded"), finish(6, level="hard"))):
        assert {k: item["data"][k] for k in e.payload.model_dump()} == e.payload.model_dump()
    await consume(event(s.GuidanceCommitted, 11, text="hidden system", source="system", truncated=False))
    assert "hidden system" not in "\n".join(_plain(line) for line in dock.tree.render(100))


def test_compacted_metadata_only_preserves_independent_pressure_status():
    p = SemanticEventProjector()
    start(p)
    p.project_result(pressure(2))
    e = event(s.ContextCompacted, 3, pre_tokens=100, post_tokens=25, summary="retained summary")
    with pytest.raises(ValueError, match="project_result"):
        p.project(e)
    result = p.project_result(e)
    assert result.events == ()
    assert result.runtime == e and result.runtime is not e
    assert result.runtime.payload is not e.payload
    assert result.runtime.model_dump() == e.model_dump()
    assert p.active_turn_count == 1
    assert p.project_result(finish(4)).events[0].pressure_id == "p"


@pytest.mark.asyncio
async def test_compacted_real_consumers_do_not_finish_or_warn(isolated_dock, configured_settings_factory):
    p = SemanticEventProjector()
    dock = isolated_dock
    dock.begin_capture()
    consumer = DockEventConsumer(dock)
    gateway = GatewaySession(lambda: dock.tree, thread_id="t")
    client = Client()
    await gateway.connect(client)

    async def consume(e):
        result = p.project_result(e)
        for old in result.events:
            pending = consumer.handle(old)
            if inspect.isawaitable(pending):
                await pending
            await gateway.broadcast_event(old)
        return result

    await consume(event(s.TurnStarted, 1, text="initial", metadata={}))
    await consume(pressure(2))
    await consume(event(s.StatusStarted, 3, status_id="independent", stage="working", description="running", parent_tool_call_id=None))
    before = ([str(line) for line in dock.tree.render(100)], list(client.messages), dict(dock._status_nodes))
    result = await consume(event(s.ContextCompacted, 4, pre_tokens=123, post_tokens=45, summary="full summary"))
    assert result.events == ()
    assert result.runtime.payload.model_dump() == dict(pre_tokens=123, post_tokens=45, summary="full summary")
    assert before == ([str(line) for line in dock.tree.render(100)], list(client.messages), dict(dock._status_nodes))
    await consume(finish(5))
    assert "independent" in dock._status_nodes
