"""Compatibility checks against the real dock and Gateway consumers."""
import inspect
import json
from uuid import uuid4

import pytest

from voidx.agent.domain import semantic_events as s
from voidx.agent.domain.turn_metadata import TurnMetadata
from voidx.presentation.adapters.semantic_event_projector import SemanticEventProjector
from voidx.presentation.gateway.session import GatewaySession
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events.consumers import DockEventConsumer
from tests.test_presentation.gateway.conftest import configured_settings_factory, isolated_dock, _plain


def event(cls, sequence, *, thread="t", turn="u", agent=None, **payload):
    return cls(event_id=uuid4(), session_id="session", thread_id=thread,
               turn_id=turn, sequence=sequence, agent_id=agent,
               parent_tool_call_id=None, timestamp=1.0, payload=payload)


def start(p, *, thread="t", turn="u", agent=None):
    return p.project(event(s.TurnStarted, 1, thread=thread, turn=turn, agent=agent,
                           text="display", metadata=TurnMetadata(profile_id="review")))


def stream(cls, seq, phase="text", **kwargs):
    return event(cls, seq, stream_id="s", phase=phase, **kwargs)


class Client:
    def __init__(self):
        self.messages = []

    async def send_text(self, text, *, priority=False):
        self.messages.append(json.loads(text))


@pytest.mark.asyncio
async def test_interleaved_phases_reach_real_dock_and_gateway(isolated_dock, configured_settings_factory):
    p = SemanticEventProjector()
    dock = isolated_dock
    dock.begin_capture()
    consumer = DockEventConsumer(dock)
    gateway = GatewaySession(lambda: dock.tree, thread_id="t")
    client = Client()
    await gateway.connect(client)
    outputs = []

    async def consume(e, **kwargs):
        projected = p.project(e, **kwargs)
        outputs.extend(projected)
        for old in projected:
            result = consumer.handle(old)
            if inspect.isawaitable(result):
                await result
            await gateway.broadcast_event(old)
        return projected

    metadata = TurnMetadata(profile_id="review", protocol="review", category="review")
    begun = await consume(event(s.TurnStarted, 1, text="display", metadata=metadata), raw_text="original")
    assert begun[0].raw_text == "original"
    assert begun[0].metadata == metadata
    assert dock.tree.root.children[0].payload["raw_text"] == "original"
    assert dock._current_turn_metadata == metadata
    turn_message = next(m for m in client.messages if m.get("method") == "turn.started")
    assert turn_message["params"]["text"] == "display"
    assert turn_message["params"]["metadata"] == metadata.model_dump(mode="json")
    await consume(stream(s.AssistantStreamStarted, 2, "thinking"))
    await consume(stream(s.AssistantChunk, 3, "thinking", delta="reason"))
    await consume(stream(s.AssistantStreamStarted, 4))
    await consume(stream(s.AssistantChunk, 5, delta="hello "))
    await consume(stream(s.AssistantChunk, 6, "thinking", delta=" more"))
    await consume(stream(s.AssistantChunk, 7, delta="world"))
    early = await consume(stream(s.AssistantCommitted, 8, text="hello world!"))
    assert not any(e.kind.endswith(".committed") for e in early)
    await consume(stream(s.AssistantCommitted, 9, "thinking", text="reason more"))
    await consume(event(s.TurnCompleted, 10, usage={}))
    assert [e.kind for e in outputs].count("assistant_stream.started") == 1
    assert [e.kind for e in outputs].count("assistant_stream.committed") == 1
    rendered = "\n".join(_plain(line) for line in dock.tree.render(100))
    assert "hello world!" in rendered
    assert "display" in rendered
    messages = client.messages
    starts = [m for m in messages if m.get("method") == "item.started" and m["params"].get("kind") == "assistant_stream"]
    assert len(starts) == 1
    deltas = [m["params"]["data"] for m in messages if m.get("method") == "item.delta" and "text" in m["params"].get("data", {})]
    assert deltas[-1]["text"] == "hello world!"
    assert p.active_turn_count == 0


def test_thinking_commit_does_not_close_stream_before_text():
    p = SemanticEventProjector()
    start(p)
    p.project(stream(s.AssistantStreamStarted, 2, "thinking"))
    assert not any(e.kind.endswith(".committed") for e in p.project(stream(s.AssistantCommitted, 3, "thinking", text="reason")))
    assert p.project(stream(s.AssistantStreamStarted, 4)) == ()
    result = p.project(stream(s.AssistantCommitted, 5, text="answer"))
    assert result[-1].kind == "assistant_stream.committed"
    assert result[-2].text == "answer"


@pytest.mark.parametrize("terminal,payload", [(s.TurnCancelled, {"reason": "stop"}), (s.TurnFailed, {"code": "E", "summary": "bad", "recoverable": False})])
@pytest.mark.asyncio
async def test_terminal_discards_real_draft(isolated_dock, configured_settings_factory, terminal, payload):
    p = SemanticEventProjector()
    isolated_dock.begin_capture()
    consumer = DockEventConsumer(isolated_dock)
    gateway = GatewaySession(lambda: isolated_dock.tree, thread_id="t")
    client = Client()
    await gateway.connect(client)
    events = [event(s.TurnStarted, 1, text="input", metadata={}),
              stream(s.AssistantStreamStarted, 2), stream(s.AssistantChunk, 3, delta="UNCOMMITTED"),
              event(terminal, 4, **payload)]
    for e in events:
        result = p.project(e)
        for old in result:
            outcome = consumer.handle(old)
            if inspect.isawaitable(outcome):
                await outcome
            await gateway.broadcast_event(old)
    assert result[0].kind == "assistant_stream.discarded"
    assert "UNCOMMITTED" not in "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
    assert any(m.get("method") == "item.completed" and m["params"]["data"].get("discarded") for m in client.messages)
    if terminal is s.TurnFailed:
        assert json.loads(result[-1].message) == payload
        assert json.loads(next(m for m in client.messages if m.get("method") == "turn.failed")["params"]["message"]) == payload
    assert p.active_turn_count == 0


def test_discarded_phase_is_not_committed_and_surviving_text_restored():
    p = SemanticEventProjector()
    start(p)
    p.project(stream(s.AssistantStreamStarted, 2, "thinking"))
    p.project(stream(s.AssistantStreamStarted, 3))
    p.project(stream(s.AssistantCommitted, 4, text="answer"))
    result = p.project(stream(s.AssistantDiscarded, 5, "thinking", reason="skip"))
    assert result[-2].text == "answer"
    assert result[-1].kind == "assistant_stream.committed"


def test_all_discarded_and_thinking_only_completion():
    p = SemanticEventProjector()
    start(p)
    p.project(stream(s.AssistantStreamStarted, 2))
    assert p.project(stream(s.AssistantDiscarded, 3, reason="stop"))[-1].kind == "assistant_stream.discarded"
    p.project(event(s.TurnCompleted, 4, usage={}))
    start(p, turn="next")
    p.project(stream(s.AssistantStreamStarted, 2, "thinking", turn="next"))
    p.project(stream(s.AssistantCommitted, 3, "thinking", turn="next", text="reason"))
    result = p.project(event(s.TurnCompleted, 4, turn="next", usage={}))
    assert result[-2].kind == "assistant_stream.committed"
    assert p.active_turn_count == 0


def test_reference_and_invalid_events_are_atomic():
    p = SemanticEventProjector()
    with pytest.raises(ValueError, match="turn"):
        p.project(stream(s.AssistantChunk, 1, delta="orphan"))
    start(p)
    with pytest.raises(ValueError):
        start(p)
    p.project(stream(s.AssistantStreamStarted, 2))
    invalid = [stream(s.AssistantChunk, 2, delta="duplicate"),
               stream(s.AssistantChunk, 4, delta="gap"),
               stream(s.AssistantChunk, 3, "thinking", delta="not started"),
               stream(s.AssistantStreamStarted, 3),
               event(s.TurnCompleted, 3, usage={}),
               stream(s.AssistantCommitted, 3, result_ref="stored"),
               event(s.AssistantStreamStarted, 3, stream_id="other", phase="text"),
               stream(s.AssistantChunk, 3, agent=4, delta="wrong agent")]
    for e in invalid:
        with pytest.raises(ValueError):
            p.project(e)
    result = p.project(stream(s.AssistantCommitted, 3, text="correct"))
    assert result[-2].text == "correct"
    with pytest.raises(ValueError):
        p.project(stream(s.AssistantChunk, 4, delta="late"))
    p.project(event(s.TurnCompleted, 4, usage={}))
    with pytest.raises(ValueError):
        p.project(event(s.TurnCompleted, 5, usage={}))


def test_unknown_kind_agent_and_raw_text_rejected_without_state():
    p = SemanticEventProjector()
    with pytest.raises(ValueError, match="project_result"):
        p.project(event(s.ContextCompacted, 1, pre_tokens=10, post_tokens=5, summary="compacted"))
    with pytest.raises(ValueError, match="agent"):
        start(p, agent="named")
    assert p.active_turn_count == 0
    assert start(p)[0].raw_text == ""
    with pytest.raises(ValueError, match="raw_text"):
        p.project(stream(s.AssistantStreamStarted, 2), raw_text="invalid")
    p.project(stream(s.AssistantStreamStarted, 2))


def test_thread_turn_isolation_and_cleanup():
    p = SemanticEventProjector()
    for thread, turn in [("a", "1"), ("a", "2"), ("b", "1")]:
        start(p, thread=thread, turn=turn)
        p.project(stream(s.AssistantStreamStarted, 2, thread=thread, turn=turn))
        p.project(stream(s.AssistantChunk, 3, thread=thread, turn=turn, delta=thread+turn))
    assert p.active_turn_count == 3
    for thread, turn in [("a", "2"), ("b", "1"), ("a", "1")]:
        result = p.project(stream(s.AssistantCommitted, 4, thread=thread, turn=turn, text=thread+turn))
        assert result[-2].text == thread+turn
        assert all(e.thread_id == thread and e.stream_id == "s" for e in result)
        p.project(event(s.TurnCompleted, 5, thread=thread, turn=turn, usage={}))
    assert p.active_turn_count == 0


@pytest.mark.asyncio
async def test_isolated_turns_reach_separate_real_consumers(configured_settings_factory):
    p = SemanticEventProjector()
    routes = {}
    try:
        for thread, turn in [("a", "1"), ("a", "2"), ("b", "1")]:
            dock = BottomInputDock()
            dock.begin_capture()
            gateway = GatewaySession(lambda dock=dock: dock.tree, thread_id=thread)
            client = Client()
            await gateway.connect(client)
            routes[thread, turn] = dock, DockEventConsumer(dock), gateway, client
        for seq, cls, payload in [
            (1, s.TurnStarted, {"text": "input", "metadata": {}}),
            (2, s.AssistantStreamStarted, {"stream_id": "s", "phase": "text"}),
            (3, s.AssistantCommitted, {"stream_id": "s", "phase": "text"}),
            (4, s.TurnCompleted, {"usage": {}}),
        ]:
            for (thread, turn), (dock, consumer, gateway, client) in routes.items():
                body = dict(payload)
                if cls is s.AssistantCommitted:
                    body["text"] = f"answer-{thread}-{turn}"
                for old in p.project(event(cls, seq, thread=thread, turn=turn, **body)):
                    result = consumer.handle(old)
                    if inspect.isawaitable(result):
                        await result
                    await gateway.broadcast_event(old)
        for (thread, turn), (dock, _, _, client) in routes.items():
            expected = f"answer-{thread}-{turn}"
            rendered = "\n".join(_plain(line) for line in dock.tree.render(100))
            assert expected in rendered
            deltas = [m["params"]["data"]["text"] for m in client.messages
                      if m.get("method") == "item.delta" and "text" in m["params"].get("data", {})]
            assert deltas and set(deltas) == {expected}
            for other_thread, other_turn in routes:
                if (other_thread, other_turn) != (thread, turn):
                    assert f"answer-{other_thread}-{other_turn}" not in rendered
        assert p.active_turn_count == 0
    finally:
        for dock, _, _, _ in routes.values():
            dock.deactivate()
            dock.reset()


@pytest.mark.asyncio
async def test_discard_one_phase_preserves_only_committed_answer(isolated_dock):
    p = SemanticEventProjector()
    isolated_dock.begin_capture()
    consumer = DockEventConsumer(isolated_dock)
    sequence = [event(s.TurnStarted, 1, text="input", metadata={}),
                stream(s.AssistantStreamStarted, 2, "thinking"),
                stream(s.AssistantChunk, 3, "thinking", delta="DISCARDED_REASON"),
                stream(s.AssistantStreamStarted, 4),
                stream(s.AssistantCommitted, 5, text="kept answer"),
                stream(s.AssistantDiscarded, 6, "thinking", reason="skip"),
                event(s.TurnCompleted, 7, usage={})]
    for e in sequence:
        for old in p.project(e):
            result = consumer.handle(old)
            if inspect.isawaitable(result):
                await result
    rendered = "\n".join(_plain(line) for line in isolated_dock.tree.render(100))
    assert "kept answer" in rendered
    assert "DISCARDED_REASON" not in rendered


@pytest.mark.asyncio
async def test_second_batch_real_tree_and_gateway(isolated_dock, configured_settings_factory):
    p = SemanticEventProjector()
    dock = isolated_dock
    dock.begin_capture()
    consumer = DockEventConsumer(dock)
    gateway = GatewaySession(lambda: dock.tree, thread_id="t")
    client = Client()
    await gateway.connect(client)
    seq = 0

    async def consume(cls, **body):
        nonlocal seq
        seq += 1
        for old in p.project(event(cls, seq, **body)):
            result = consumer.handle(old)
            if inspect.isawaitable(result):
                await result
            await gateway.broadcast_event(old)

    await consume(s.TurnStarted, text="request", metadata=TurnMetadata())
    await consume(s.ToolStarted, tool_call_id="edit", name="write", arguments={"file_path": "a.py"})
    await consume(s.AssistantStreamStarted, stream_id="answer", phase="text")
    await consume(s.AssistantChunk, stream_id="answer", phase="text", delta="working answer")
    await consume(s.StatusStarted, status_id="st", stage="working", description="Editing", parent_tool_call_id="edit")
    await consume(s.StatusUpdated, status_id="st", stage="working", description="Finishing", parent_tool_call_id="edit")
    await consume(s.StatusFinished, status_id="st", stage="working", description="Finished", parent_tool_call_id="edit", ok=True, result="done")
    await consume(s.ToolFinished, tool_call_id="edit", name="write", elapsed=.1, ok=False)
    await consume(s.ToolResult, tool_call_id="edit", name="write", summary="WRITE_FAILED")
    diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old_value\n+new_value\n"
    await consume(s.FileChanged, tool_call_id="edit", path="a.py", diff=diff)
    await consume(s.AssistantCommitted, stream_id="answer", phase="text", text="final answer")
    body = dict(items=[dict(id="task", content="VISIBLE_TASK", status="pending")], operation="write", boundary_id="b")
    await consume(s.TodoUpdated, **body)
    assert dock._todo_state.items[0].content == "VISIBLE_TASK"
    await consume(s.TodoCommitted, **body)
    rendered = "\n".join(_plain(line) for line in dock.tree.render(100))
    snapshot = next(node for node in dock.tree.root.children if node.node_type == "todo")
    assert snapshot.payload["items"][0]["content"] == "VISIBLE_TASK"
    assert snapshot.status == "done"
    assert dock.todo_state() is None
    assert "WRITE_FAILED" in rendered
    assert "new_value" in rendered and "old_value" in rendered
    assert "final answer" in rendered
    await consume(s.TodoUpdated, **{**body, "boundary_id": "preview-to-clear"})
    assert dock.todo_state() is not None
    await consume(s.TodoCleared, items=[], operation="clear", boundary_id="clear")
    assert snapshot in dock.tree.root.children
    assert dock._todo_state is None
    await consume(s.TurnCompleted, usage={})
    wire = json.dumps(client.messages, ensure_ascii=False)
    for text in ("WRITE_FAILED", "new_value", "old_value", "VISIBLE_TASK", "final answer"):
        assert text in wire
    assert '"cleared": true' in wire
    assert '"ok": false' in wire
    assert p.active_turn_count == 0


@pytest.mark.asyncio
async def test_hidden_agent_failure_promotes_real_consumers(isolated_dock, configured_settings_factory):
    from voidx.agent.domain.display_policy import ToolDisplayPolicy, ToolDisplayRule

    p = SemanticEventProjector(display_policy=ToolDisplayPolicy(rules={
        "agent": ToolDisplayRule(tool_name="agent", mode="hidden")}))
    dock = isolated_dock
    dock.begin_capture()
    consumer = DockEventConsumer(dock)
    gateway = GatewaySession(lambda: dock.tree, thread_id="t")
    client = Client()
    await gateway.connect(client)
    sequence = [event(s.TurnStarted, 1, text="request", metadata=TurnMetadata()),
                event(s.ToolStarted, 2, tool_call_id="a", name="agent", arguments={}),
                event(s.ToolFinished, 3, tool_call_id="a", name="agent", elapsed=.1, ok=False),
                event(s.ToolResult, 4, tool_call_id="a", name="agent", summary="AGENT_FAILURE")]
    for e in sequence:
        for old in p.project(e):
            result = consumer.handle(old)
            if inspect.isawaitable(result):
                await result
            await gateway.broadcast_event(old)
    assert "AGENT_FAILURE" in "\n".join(_plain(line) for line in dock.tree.render(100))
    assert "AGENT_FAILURE" in json.dumps(client.messages)


def test_legacy_api_rejects_request_without_advancing_turn():
    from tests.test_presentation.adapters.test_semantic_interaction_projector import required

    p = SemanticEventProjector()
    start(p)
    with pytest.raises(ValueError, match="project_result"):
        p.project(required())
    result = p.project_result(required())
    assert result.request.request_id == "i"
    assert p.pending_interaction_count == 1


@pytest.mark.parametrize("terminal,payload,kind", [
    (s.TurnCompleted, {"usage": {}}, "turn.completed"),
    (s.TurnFailed, {"code": "BROKEN", "summary": "failed", "recoverable": False}, "turn.failed"),
    (s.TurnCancelled, {"reason": "stop"}, "turn.cancelled"),
])
def test_all_terminals_require_real_resolution_without_state_pollution(terminal, payload, kind):
    from tests.test_presentation.adapters.test_semantic_interaction_projector import required, resolved

    p = SemanticEventProjector()
    start(p)
    p.project_result(required())
    p.project(stream(s.AssistantStreamStarted, 3))
    for _ in range(2):
        with pytest.raises(ValueError, match="pending interactions"):
            p.project_result(event(terminal, 4, **payload))
        assert p.active_turn_count == p.pending_interaction_count == 1
    source = resolved(4)
    accepted = p.project_result(source)
    assert accepted.resolved == source
    discarded = p.project(stream(s.AssistantDiscarded, 5, reason="discard"))
    assert discarded[-1].kind == "assistant_stream.discarded"
    tail = p.project_result(event(terminal, 6, **payload))
    assert [e.kind for e in tail.events] == [kind]
    assert tail.resolved is None
    assert accepted.resolved.payload.resolution.decision == "approved"
    assert accepted.resolved.payload.resolution.resolution_reason == "answered"
    assert p.active_turn_count == p.pending_interaction_count == 0


def test_legacy_api_rejects_resolution_without_advancing_or_losing_metadata():
    from tests.test_presentation.adapters.test_semantic_interaction_projector import required, resolved

    p = SemanticEventProjector()
    start(p)
    p.project_result(required())
    source = resolved()
    with pytest.raises(ValueError, match="project_result"):
        p.project(source)
    assert p.pending_interaction_count == 1
    result = p.project_result(source)
    assert result.resolved == source
    assert result.events == ()
    assert p.pending_interaction_count == 0
