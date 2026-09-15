"""Owner-envelope compatibility with explicit, stable legacy display identities."""
import inspect

import pytest

from voidx.agent.domain import semantic_events as s
from voidx.presentation.adapters.semantic_event_projector import SemanticEventProjector
from voidx.presentation.gateway.session import GatewaySession
from voidx.presentation.output.events.consumers import DockEventConsumer
from tests.test_presentation.adapters.test_semantic_event_projector import Client, event, start
from tests.test_presentation.gateway.conftest import configured_settings_factory, isolated_dock


def child(cls, seq, **kwargs):
    fields = dict(subagent_id="child", parent_agent_id=None,
                  parent_tool_call_id=None, description="review changes")
    fields.update(kwargs)
    return event(cls, seq, **fields)


def projector(resolver=None):
    return SemanticEventProjector(subagent_resolver=resolver or (lambda e: (7, "reviewer")))


def test_explicit_resolver_and_metadata_required():
    p = SemanticEventProjector()
    start(p)
    e = child(s.SubagentStarted, 2)
    with pytest.raises(ValueError, match="project_result"):
        p.project(e)
    with pytest.raises(ValueError, match="resolver"):
        p.project_result(e)


def test_resolved_once_stable_detached_metadata_and_order():
    calls = []
    p = projector(lambda e: (calls.append(e.model_dump()) or (7, "reviewer")))
    start(p)
    for cls, seq, extra in [(s.SubagentStarted, 2, {}),
                            (s.SubagentStepStarted, 3, dict(step_id="step-1")),
                            (s.SubagentFinished, 4, dict(reason="error", summary="failed detail", ok=False))]:
        e = child(cls, seq, **extra)
        r = p.project_result(e)
        assert r.subagent == e and r.subagent is not e
        assert r.subagent.payload is not e.payload
        assert r.subagent.model_dump() == e.model_dump()
        old = r.events[0]
        assert old.agent_id == 7 and old.subagent_id == "child"
        if cls is not s.SubagentFinished:
            assert old.name == "reviewer"
        else:
            assert (old.finish_reason, old.summary, old.error, old.ok) == ("error", "failed detail", "", False)
            assert old.elapsed is old.calls is old.tokens is None
    assert len(calls) == 1
    with pytest.raises(ValueError):
        p.project_result(child(s.SubagentFinished, 5, reason="error", summary="", ok=False))
    p.project(event(s.TurnCompleted, 5, usage={}))


@pytest.mark.parametrize("cls,extra", [(s.SubagentStepStarted, {"step_id": "x"}),
                                      (s.SubagentFinished, {"reason": "done", "summary": "", "ok": True})])
def test_orphans_rejected_transactionally(cls, extra):
    p = projector()
    start(p)
    with pytest.raises(ValueError):
        p.project_result(child(cls, 2, **extra))
    p.project_result(child(s.SubagentStarted, 2))


@pytest.mark.parametrize("bad", [{"parent_agent_id": 42}, {"parent_tool_call_id": "missing"}, {"agent": 7}])
def test_parent_and_owner_validation_without_pollution(bad):
    calls = []
    p = projector(lambda e: (calls.append(e) or (7, "reviewer")))
    start(p)
    with pytest.raises(ValueError):
        p.project_result(child(s.SubagentStarted, 2, **bad))
    assert not calls
    p.project_result(child(s.SubagentStarted, 2))


@pytest.mark.parametrize("identity", [(True, "x"), (-1, "x"), (7, ""), (None, "x")])
def test_invalid_resolver_identity_retry(identity):
    values = iter([identity, (7, "reviewer")])
    p = projector(lambda e: next(values))
    start(p)
    with pytest.raises(ValueError):
        p.project_result(child(s.SubagentStarted, 2))
    p.project_result(child(s.SubagentStarted, 2))


def test_duplicates_steps_and_cross_scope_isolation():
    p = projector(lambda e: (7 if e.thread_id == "t" else 8, "reviewer"))
    start(p)
    start(p, thread="other", turn="other")
    p.project_result(child(s.SubagentStarted, 2))
    with pytest.raises(ValueError):
        p.project_result(child(s.SubagentStarted, 3))
    # Legacy Gateway keys by subagent_id; concurrent collisions must be refused.
    with pytest.raises(ValueError):
        p.project_result(child(s.SubagentStarted, 2, thread="other", turn="other"))
    p.project_result(child(s.SubagentStarted, 2, thread="other", turn="other", subagent_id="other"))
    with pytest.raises(ValueError):
        p.project_result(child(s.SubagentStepStarted, 3, thread="other", turn="other", step_id="s"))
    p.project_result(child(s.SubagentStepStarted, 3, step_id="s"))
    with pytest.raises(ValueError):
        p.project_result(child(s.SubagentStepStarted, 4, step_id="s"))
    p.project_result(child(s.SubagentStepStarted, 4, step_id="next"))


@pytest.mark.parametrize("cls,payload", [(s.TurnCompleted, {"usage": {}}),
    (s.TurnFailed, {"code": "ERR", "summary": "failed", "recoverable": False}),
    (s.TurnCancelled, {"reason": "cancelled"})])
def test_all_terminal_require_observed_child_finish(cls, payload):
    p = projector()
    start(p)
    p.project_result(child(s.SubagentStarted, 2))
    with pytest.raises(ValueError, match="pending subagent"):
        p.project_result(event(cls, 3, **payload))
    assert p.active_turn_count == 1
    p.project_result(child(s.SubagentFinished, 3, reason="cancelled", summary="stopped", ok=False))
    result = p.project_result(event(cls, 4, **payload))
    assert [e.kind for e in result.events] == [cls.model_fields["kind"].default]
    assert p.active_turn_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,ok,status", [("final_answer", True, "done"), ("error", False, "error"), ("cancelled", False, "done")])
async def test_real_consumers_tool_reuse_steps_finish(isolated_dock, configured_settings_factory, reason, ok, status):
    p = projector()
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
    await consume(event(s.ToolStarted, 2, tool_call_id="call", name="agent", arguments={}))
    canonical = consumer._tool_nodes["call"]
    await consume(child(s.SubagentStarted, 3, parent_tool_call_id="call"))
    assert consumer._agent_nodes[7] is canonical
    assert canonical.parent is not None
    await consume(child(s.SubagentStepStarted, 4, parent_tool_call_id="call", step_id="step"))
    assert "agent:7:progress" in dock._status_nodes
    r = await consume(child(s.SubagentFinished, 5, parent_tool_call_id="call", reason=reason, summary="report", ok=ok))
    assert canonical.status == status and canonical.elapsed is None
    assert r.events[0].calls is r.events[0].tokens is None
    items = [m["params"] for m in client.messages if m.get("method", "").startswith("item.") and m["params"].get("kind") == "subagent"]
    assert len(items) == 3 and len({i["item_id"] for i in items}) == 1
    assert items[0]["data"]["name"] == "reviewer"
    assert items[1]["data"]["step"] is True
    assert items[2]["data"] == dict(subagent_id="child", ok=ok, elapsed=None, summary="report", error="")


@pytest.mark.parametrize("change", [{"parent_tool_call_id": "other"}, {"description": "changed"}, {"parent_agent_id": 7}])
def test_lifecycle_parent_identity_cannot_change(change):
    p = projector()
    start(p)
    p.project_result(child(s.SubagentStarted, 2))
    with pytest.raises(ValueError):
        p.project_result(child(s.SubagentStepStarted, 3, step_id="s", **change))
    p.project_result(child(s.SubagentStepStarted, 3, step_id="s"))


def test_resolver_failure_and_child_id_collision_leave_retry_position():
    def resolve(e):
        if not ready:
            raise RuntimeError("identity unavailable")
        return (7, "reviewer")
    ready = False
    p = projector(resolve)
    start(p)
    with pytest.raises(RuntimeError, match="identity unavailable"):
        p.project_result(child(s.SubagentStarted, 2))
    ready = True
    p.project_result(child(s.SubagentStarted, 2))
    with pytest.raises(ValueError, match="colliding"):
        p.project_result(child(s.SubagentStarted, 3, subagent_id="second"))
    p.project_result(child(s.SubagentStepStarted, 3, step_id="s"))


@pytest.mark.parametrize("scope", [{"turn": "other"}, {"thread": "other"}])
def test_same_owner_cross_turn_or_thread_cannot_finish_foreign_child(scope):
    p = projector()
    start(p)
    start(p, **scope)
    p.project_result(child(s.SubagentStarted, 2))
    with pytest.raises(ValueError):
        p.project_result(child(s.SubagentFinished, 2, reason="done", summary="", ok=True, **scope))
    p.project_result(event(s.TurnCompleted, 2, usage={}, **scope))
    p.project_result(child(s.SubagentFinished, 3, reason="done", summary="", ok=True))


def test_non_root_owner_and_parent_tool_envelope_validation():
    p = projector()
    start(p, agent=3)
    e = child(s.SubagentStarted, 2, agent=3, parent_agent_id=3)
    with pytest.raises(ValueError):
        p.project_result(e.model_copy(update={"parent_tool_call_id": "wrong"}))
    old = p.project_result(e).events[0]
    assert old.parent_agent_id == 3 and old.agent_id == 7
    with pytest.raises(ValueError, match="agent_id changed"):
        p.project_result(event(s.AssistantStreamStarted, 3, agent=7, stream_id="child-output", phase="text"))
