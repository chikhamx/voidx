"""Pure HITL projection and legacy consumer compatibility."""
import inspect
import json

import pytest

from voidx.agent.domain import semantic_events as s
from voidx.presentation.adapters.semantic_event_projector import SemanticEventProjector
from voidx.presentation.protocol.requests import parse_ui_request
from voidx.presentation.gateway.session import GatewaySession
from voidx.presentation.output.events.consumers import DockEventConsumer
from tests.test_presentation.adapters.test_semantic_event_projector import event, start, Client
from tests.test_presentation.gateway.conftest import configured_settings_factory, isolated_dock, _plain


def required(seq=2, purpose="generic", kind="choice", iid="i", **fields):
    request = dict(interaction_id=iid, session_id="session", thread_id="t", turn_id="u",
                   purpose=purpose, input_kind=kind, prompt="Question?",
                   choices=[dict(label="Yes", value="yes", description="Proceed")])
    request.update(fields)
    return event(s.InteractionRequired, seq, request=request)


def resolved(seq=3, purpose="generic", iid="i", **fields):
    resolution = dict(value="yes", decision="approved", resolution_reason="answered")
    resolution.update(fields)
    return event(s.InteractionResolved, seq, interaction_id=iid, purpose=purpose, resolution=resolution)


CARDS = [("generic", {}), ("permission", {"tools": [dict(name="bash", pattern="ls", args={"cmd": "ls"}, risk={"level": "low"}, allowed_scopes=["once"], default_scope="once")], "allowed_scopes": ["once"]}),
         ("checkpoint", {"checkpoint": dict(goal="Ship", steps=["Test"], affected_files=["a"], risks=["risk"])}),
         ("clarify", {}), ("goal", {"goal": dict(objective="Ship", acceptance_condition="Green", achievement_method="TDD", max_attempts=3)}),
         ("loop", {"loop": dict(prompt="Check", interval_seconds=12.5)})]


RESOLVED_KINDS = {
    "generic": [], "permission": ["permission_prompt.cleared"],
    "checkpoint": ["checkpoint_decision.submitted"],
    "clarify": ["clarify_answer.submitted"],
    "goal": ["goal_spec_decision.submitted"],
    "loop": ["loop_spec_decision.submitted"],
}


@pytest.mark.parametrize("purpose,fields", CARDS)
@pytest.mark.asyncio
async def test_cards_requests_and_real_consumers(purpose, fields, isolated_dock, configured_settings_factory):
    p = SemanticEventProjector()
    dock = isolated_dock
    dock.begin_capture()
    consumer = DockEventConsumer(dock)
    gateway = GatewaySession(lambda: dock.tree, thread_id="t")
    client = Client()
    await gateway.connect(client)
    initial = start(p)
    kind = "permission" if purpose == "permission" else "choice"
    result = p.project_result(required(purpose=purpose, kind=kind, timeout=17, allow_free_text=True, **fields))
    assert result.interaction.timeout == 17
    assert result.interaction.allow_free_text
    request = result.request
    assert request.request_id == "i" and request.thread_id == "t"
    assert request.choices == [("Yes", "yes", "Proceed")]
    assert parse_ui_request(json.loads(request.model_dump_json())) == request
    if purpose == "permission":
        assert request.tools[0].model_dump()["args"] == {"cmd": "ls"}
        assert request.tools[0].allowed_scopes == ("once",)
    tail = p.project_result(resolved(purpose=purpose, value="custom", free_text=True))
    for old in (*initial, *result.events, *tail.events):
        value = consumer.handle(old)
        if inspect.isawaitable(value):
            await value
        await gateway.broadcast_event(old)
    rendered = "\n".join(_plain(line) for line in dock.tree.render(100))
    assert [e.kind for e in tail.events] == RESOLVED_KINDS[purpose]
    visible_answer = purpose not in {"generic", "permission"}
    assert ("custom" in rendered) == visible_answer
    assert ("custom" in json.dumps(client.messages)) == (purpose in {"checkpoint", "clarify"})
    if purpose in {"goal", "loop"}:
        completed = [m["params"] for m in client.messages if m.get("method") == "item.completed"]
        assert completed[-1]["kind"] == "prompt"
        assert completed[-1]["data"] == {
            "prompt_type": f"{purpose}_spec", "prompt_id": "i", "decision": "approved",
        }
    assert "warning.appended" not in [e.kind for e in (*result.events, *tail.events)]
    if purpose == "checkpoint":
        card = tail.events[0]
        assert (card.checkpoint_id, card.decision, card.label, card.response, card.was_custom_input) == ("i", "approved", "custom", "custom", True)
    elif purpose in {"goal", "loop"}:
        card = tail.events[0]
        assert (card.prompt_id, card.decision, card.response) == ("i", "approved", "custom")
    elif purpose == "clarify":
        card = tail.events[0]
        assert (card.clarify_id, card.answer, card.cancelled, card.was_custom_input) == ("i", "custom", False, True)
    elif purpose == "permission":
        assert tail.events[0].request_id == "i"
    assert p.pending_interaction_count == 0
    assert tail.resolved.payload.resolution.resolution_reason == "answered"
    gateway.disconnect(client)


@pytest.mark.parametrize("reason", ["answered", "user_rejected", "dismissed", "timed_out", "task_cancelled"])
@pytest.mark.parametrize("purpose,fields", CARDS)
def test_resolution_reasons_preserved(reason, purpose, fields):
    p = SemanticEventProjector(); start(p)
    p.project_result(required(purpose=purpose, kind="permission" if purpose == "permission" else "choice", **fields))
    result = p.project_result(resolved(purpose=purpose, decision="deny" if purpose == "permission" else "rejected", value="no", resolution_reason=reason))
    dumped = json.dumps([e.model_dump() for e in result.events])
    assert [e.kind for e in result.events] == RESOLVED_KINDS[purpose]
    assert result.resolved.payload.resolution.resolution_reason == reason
    assert result.resolved.payload.resolution.value == "no"
    assert "approved" not in dumped
    assert result.request is None
    assert p.pending_interaction_count == 0


def test_text_defaults_secret_and_detachment():
    p = SemanticEventProjector(); start(p)
    e = required(kind="text", choices=[], default_value="seed", secret=True)
    result = p.project_result(e)
    assert json.loads(result.request.model_dump_json()) == dict(kind="text", request_id="i", thread_id="t", prompt="Question?", default="seed", secret=True)
    assert result.interaction == e.payload.request
    assert result.interaction is not e.payload.request
    result.interaction.purpose = "goal"
    e.payload.request.purpose = "loop"
    assert p.project_result(resolved()).request is None


@pytest.mark.parametrize("fields", [dict(kind="text"), dict(default_value="lost"), dict(secret=True), dict(purpose="goal"), dict(purpose="generic", checkpoint=dict(goal="extra")), dict(kind="permission"), dict(tools=[dict(name="bash")])])
def test_unrepresentable_rejected_transactionally(fields):
    p = SemanticEventProjector(); start(p)
    with pytest.raises(ValueError):
        p.project_result(required(**fields))
    assert p.pending_interaction_count == 0
    p.project_result(required())


def test_duplicates_orphans_purpose_agent_and_capacity_are_transactional():
    p = SemanticEventProjector(); start(p)
    with pytest.raises(ValueError):
        p.project_result(resolved(seq=2))
    for seq in range(2, 66):
        p.project_result(required(seq, iid=str(seq)))
    assert p.pending_interaction_count == 64
    for bad in [required(66, iid="2"), required(66, iid="overflow"), resolved(66, iid="2", purpose="goal"), resolved(66, iid="missing"), resolved(66, iid="2").model_copy(update={"agent_id": 1})]:
        with pytest.raises(ValueError):
            p.project_result(bad)
        assert p.pending_interaction_count == 64
    p.project_result(resolved(66, iid="2"))
    with pytest.raises(ValueError):
        p.project_result(required(67, iid="2"))
    p.project_result(required(67, iid="new"))


def test_terminal_cleanup_and_interleaving():
    p = SemanticEventProjector(); start(p)
    start(p, thread="other")
    p.project_result(required(purpose="goal", goal=dict(objective="Ship", acceptance_condition="Green")))
    other = required(iid="other-id").model_dump()
    other["thread_id"] = other["payload"]["request"]["thread_id"] = "other"
    p.project_result(s.InteractionRequired(**other))
    with pytest.raises(ValueError):
        p.project_result(event(s.TurnCompleted, 3, usage={}))
    with pytest.raises(ValueError, match="pending interactions"):
        p.project_result(event(s.TurnCancelled, 3, reason="stop"))
    assert p.pending_interaction_count == 2
    decision = p.project_result(resolved(3, purpose="goal"))
    result = p.project_result(event(s.TurnCancelled, 4, reason="stop"))
    assert result.events[-1].kind == "turn.cancelled"
    assert [e.kind for e in result.events] == ["turn.cancelled"]
    assert decision.resolved.payload.resolution.decision == "approved"
    assert p.pending_interaction_count == 1
    with pytest.raises(ValueError):
        p.project_result(resolved(4, purpose="goal"))
    p.project_result(resolved(iid="other-id").model_copy(update={"thread_id": "other"}))
    assert p.pending_interaction_count == 0


def test_secret_card_is_rejected_before_it_can_become_pending():
    p = SemanticEventProjector(); start(p)
    with pytest.raises(ValueError, match="Secret"):
        p.project_result(required(purpose="clarify", kind="text", choices=[], secret=True))
    assert p.pending_interaction_count == 0
    p.project_result(required(kind="text", choices=[], secret=True))
    result = p.project_result(resolved(value="private-value"))
    assert "private-value" not in json.dumps([e.model_dump() for e in result.events])


def test_permission_single_slot_rejects_overlap_without_advancing():
    p = SemanticEventProjector(); start(p)
    p.project_result(required(purpose="permission", kind="permission"))
    with pytest.raises(ValueError, match="permission"):
        p.project_result(required(3, purpose="permission", kind="permission", iid="second"))
    p.project_result(resolved(3, purpose="permission"))
    p.project_result(required(4, purpose="permission", kind="permission", iid="second"))


def test_legacy_ids_cannot_collide_across_active_turns():
    p = SemanticEventProjector(); start(p)
    start(p, thread="other")
    p.project_result(required())
    other = required().model_dump()
    other["thread_id"] = other["payload"]["request"]["thread_id"] = "other"
    with pytest.raises(ValueError, match="interaction_id"):
        p.project_result(s.InteractionRequired(**other))
    other["payload"]["request"]["interaction_id"] = "other-id"
    p.project_result(s.InteractionRequired(**other))


@pytest.mark.parametrize("terminal,payload", [(s.TurnCompleted, dict(usage={})), (s.TurnCancelled, dict(reason="stop")), (s.TurnFailed, dict(code="BROKEN", summary="failed", recoverable=False))])
@pytest.mark.asyncio
async def test_terminal_transcript_tail_reaches_real_consumers(terminal, payload, isolated_dock, configured_settings_factory):
    p = SemanticEventProjector()
    dock = isolated_dock
    dock.begin_capture()
    consumer = DockEventConsumer(dock)
    gateway = GatewaySession(lambda: dock.tree, thread_id="t")
    client = Client()
    await gateway.connect(client)
    outputs = list(start(p))
    for seq, (purpose, fields) in enumerate(CARDS, 2):
        result = p.project_result(required(seq, purpose=purpose, iid=purpose,
            kind="permission" if purpose == "permission" else "choice", **fields))
        outputs.extend(result.events)
        if purpose == "checkpoint":
            assert result.events[0].plan.model_dump() == fields["checkpoint"]
        elif purpose in {"goal", "loop"}:
            assert result.events[0].spec.model_dump() == fields[purpose]
    with pytest.raises(ValueError, match="pending interactions"):
        p.project_result(event(terminal, 8, **payload))
    for seq, (purpose, _) in enumerate(CARDS, 8):
        resolution = p.project_result(resolved(seq, purpose=purpose, iid=purpose,
            decision="cancelled", resolution_reason="task_cancelled", value=""))
        assert [e.kind for e in resolution.events] == RESOLVED_KINDS[purpose]
        assert resolution.resolved.payload.resolution.resolution_reason == "task_cancelled"
        outputs.extend(resolution.events)
    tail = p.project_result(event(terminal, 14, **payload))
    assert len(tail.events) == 1
    assert tail.resolved is None
    assert tail.events[-1].kind == {s.TurnCompleted: "turn.completed", s.TurnCancelled: "turn.cancelled", s.TurnFailed: "turn.failed"}[terminal]
    outputs.extend(tail.events)
    for old in outputs:
        value = consumer.handle(old)
        if inspect.isawaitable(value):
            await value
        await gateway.broadcast_event(old)
    from voidx.presentation.protocol.transcript import TranscriptNode

    def assert_snapshot(messages):
        snapshot = messages[-1]["params"]["active_snapshot"]
        by_id = {node["id"]: node for node in snapshot["nodes"]}
        cards = []
        def visit(node):
            if node.node_type in {"checkpoint", "clarify", "goal_spec", "loop_spec"}:
                cards.append(node)
            for child in node.children:
                visit(child)
        visit(dock.tree.root)
        assert {node.node_type for node in cards} == {"checkpoint", "clarify", "goal_spec", "loop_spec"}
        for card in cards:
            expected = {key: getattr(card, key) for key in TranscriptNode.model_fields
                        if key not in {"node_type", "parent_id", "title", "child_ids"}}
            expected.update(node_type="checkpoint", parent_id=card.parent.id,
                            title=card.header, child_ids=[child.id for child in card.children])
            assert by_id[card.id] == expected
            for child in card.children:
                assert by_id[child.id]["parent_id"] == card.id
                assert by_id[child.id]["body_lines"] == child.body_lines
        return snapshot["nodes"]

    terminal_nodes = assert_snapshot(client.messages)
    reconnected = Client()
    await gateway.connect(reconnected)
    assert assert_snapshot(reconnected.messages) == terminal_nodes
    gateway.disconnect(reconnected)
    rendered = "\n".join(_plain(line) for line in dock.tree.render(100))
    assert "task_cancelled" not in rendered
    assert "approved" not in rendered
    assert "permission_prompt.cleared" in [e.kind for e in outputs]
    assert "warning.appended" not in [e.kind for e in outputs]
    assert "task_cancelled" not in json.dumps(client.messages)
    assert p.pending_interaction_count == p.active_turn_count == 0
    gateway.disconnect(client)


@pytest.mark.asyncio
async def test_secret_resolution_metadata_is_complete_detached_and_not_rendered(isolated_dock, configured_settings_factory):
    p = SemanticEventProjector()
    dock = isolated_dock
    dock.begin_capture()
    consumer = DockEventConsumer(dock)
    gateway = GatewaySession(lambda: dock.tree, thread_id="t")
    client = Client()
    await gateway.connect(client)
    initial = start(p)
    req = p.project_result(required(kind="text", choices=[], secret=True))
    source = resolved(value="private-answer", free_text=True, scope="once").model_copy(
        update={"parent_tool_call_id": "parent-tool"})
    result = p.project_result(source)
    assert result.events == ()
    assert result.request is result.interaction is None
    assert result.resolved == source
    assert result.resolved is not source
    assert result.resolved.payload is not source.payload
    assert result.resolved.payload.resolution is not source.payload.resolution
    source.payload.resolution.value = "source-mutated"
    assert result.resolved.payload.resolution.value == "private-answer"
    result.resolved.payload.resolution.decision = "metadata-mutated"
    assert source.payload.resolution.decision == "approved"
    for old in (*initial, *req.events, *result.events):
        value = consumer.handle(old)
        if inspect.isawaitable(value):
            await value
        await gateway.broadcast_event(old)
    assert "private-answer" not in "\n".join(_plain(line) for line in dock.tree.render(100))
    assert "private-answer" not in json.dumps(client.messages)
    assert p.pending_interaction_count == 0
    gateway.disconnect(client)


def checkpoint_required(seq=2, iid="card", stage="decision", cid="card"):
    return required(seq, purpose="checkpoint", kind="text" if stage == "scope" else "choice",
        iid=iid, checkpoint_id=cid, checkpoint_stage=stage, checkpoint=dict(goal="Ship"),
        choices=[] if stage == "scope" else [dict(label="Modify", value="modified")])


def test_checkpoint_two_stages_share_one_card():
    p = SemanticEventProjector(); start(p)
    first = p.project_result(checkpoint_required())
    choice = p.project_result(resolved(3, "checkpoint", "card", value="modified", decision="modified"))
    scope = p.project_result(checkpoint_required(4, "scope", "scope"))
    final = p.project_result(resolved(5, "checkpoint", "scope", value="approved", decision="modified", free_text=True))
    assert len(first.events) == 1
    assert scope.events == (), "scope must not show a second checkpoint card"
    assert choice.events[0].checkpoint_id == final.events[0].checkpoint_id == "card"
    assert choice.resolved.payload.resolution.value == "modified"
    assert final.resolved.payload.resolution.value == "approved"


@pytest.mark.parametrize("prior", ["missing", "pending", "approved", "free_text", "consumed"])
def test_checkpoint_scope_requires_one_resolved_modified_choice(prior):
    p = SemanticEventProjector(); start(p)
    seq = 2
    if prior != "missing":
        p.project_result(checkpoint_required()); seq = 3
        if prior != "pending":
            p.project_result(resolved(3, "checkpoint", "card", value="modified",
                decision="approved" if prior == "approved" else "modified", free_text=prior == "free_text"))
            seq = 4
        if prior == "consumed":
            p.project_result(checkpoint_required(4, "scope", "scope"))
            p.project_result(resolved(5, "checkpoint", "scope", decision="modified", value="scope", free_text=True))
            seq = 6
    from copy import deepcopy
    before = deepcopy(p._turns)
    with pytest.raises(ValueError, match="scope"):
        p.project_result(checkpoint_required(seq, "invalid", "scope"))
    assert p._turns == before


def test_checkpoint_association_capacity_is_transactional():
    from copy import deepcopy
    p = SemanticEventProjector(); start(p)
    seq = 2
    for n in range(64):
        cid = f"card-{n}"
        p.project_result(checkpoint_required(seq, cid, cid=cid))
        p.project_result(resolved(seq + 1, "checkpoint", cid, value="modified", decision="modified"))
        seq += 2
    before = deepcopy(p._turns)
    with pytest.raises(ValueError, match="64"):
        p.project_result(checkpoint_required(seq, "overflow", cid="overflow"))
    assert p._turns == before
    p.project_result(checkpoint_required(seq, "scope", "scope", "card-0"))


def test_checkpoint_association_revalidates_mutated_request():
    from copy import deepcopy
    p = SemanticEventProjector(); start(p)
    source = checkpoint_required()
    source.payload.request.checkpoint_id = "different-card"
    before = deepcopy(p._turns)
    with pytest.raises(ValueError, match="Checkpoint"):
        p.project_result(source)
    assert p._turns == before


def test_checkpoint_scope_cannot_borrow_another_turn():
    p = SemanticEventProjector(); start(p)
    p.project_result(checkpoint_required())
    p.project_result(resolved(3, "checkpoint", "card", value="modified", decision="modified"))
    start(p, turn="other")
    source = checkpoint_required(2, "scope", "scope")
    source = source.model_copy(update={"turn_id": "other"})
    source.payload.request.turn_id = "other"
    with pytest.raises(ValueError, match="scope"):
        p.project_result(source)
    source = source.model_copy(update={"turn_id": "u", "sequence": 4})
    source.payload.request.turn_id = "u"
    assert p.project_result(source).events == ()


def test_checkpoint_request_ownership_mutation_is_transactional():
    from copy import deepcopy
    p = SemanticEventProjector(); start(p)
    source = checkpoint_required()
    source.payload.request.turn_id = "other"
    before = deepcopy(p._turns)
    with pytest.raises(ValueError, match="ownership"):
        p.project_result(source)
    assert p._turns == before
