"""Semantic event wire contracts, including hostile JSON and isolation."""
import json
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from voidx.agent.domain import semantic_events as events
from voidx.agent.domain.turn_metadata import TurnMetadata


STREAM = {"stream_id": "s", "phase": "text"}
TOOL = {"tool_call_id": "tc", "name": "read"}
STATUS = {"status_id": "st", "stage": "working", "description": "working", "parent_tool_call_id": None}
TODO = {"items": [{"id": "1", "content": "work", "status": "pending"}], "operation": "write", "boundary_id": "b"}
SUB = {"subagent_id": "a", "parent_agent_id": None, "parent_tool_call_id": "tc", "description": "inspect"}
PAYLOADS = {
    "turn.started": {"text": "hi", "metadata": {}},
    "turn.completed": {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}},
    "turn.failed": {"code": "model_error", "summary": "failed", "recoverable": False},
    "turn.cancelled": {"reason": "user"},
    "assistant.stream_started": STREAM,
    "assistant.chunk": {**STREAM, "delta": "你好😀"},
    "assistant.committed": {**STREAM, "text": "done"},
    "assistant.discarded": {**STREAM, "reason": "retry"},
    "tool.started": {**TOOL, "arguments": {"nested": [{"a": 1}]}},
    "tool.finished": {**TOOL, "elapsed": 0.1, "ok": True},
    "tool.result": {**TOOL, "summary": "done", "result_ref": "store:123"},
    "file.changed": {"tool_call_id": "tc", "path": "a.py", "diff": "+ok"},
    "status.started": STATUS,
    "status.updated": STATUS,
    "status.finished": {**STATUS, "ok": True, "result": "done"},
    "todo.updated": TODO,
    "todo.committed": TODO,
    "todo.cleared": {**TODO, "items": [], "operation": "clear"},
    "subagent.started": SUB,
    "subagent.step_started": {**SUB, "step_id": "step"},
    "subagent.finished": {**SUB, "reason": "completed", "summary": "done", "ok": True},
    "diagnostic.warning": {"code": "budget", "summary": "near limit", "recoverable": True},
    "diagnostic.error": {"code": "model_error", "summary": "failed", "recoverable": False},
    "context.pressure_updated": {"pressure_id": "p", "level": "soft", "action": "converge_hint", "outcome": "hint_injected", "reason": "budget", "can_compact": True, "turn_count": 1, "pre_tokens": 10, "soft_threshold": 8, "hard_threshold": 20},
    "context.pressure_finished": {"pressure_id": "p", "level": "soft", "outcome": "compacted", "detail": "done", "ok": True},
    "context.compacted": {"pre_tokens": 10, "post_tokens": 5, "summary": "done"},
    "guidance.submitted": {"text": "help", "source": "user", "truncated": False},
    "guidance.committed": {"text": "help", "source": "user", "truncated": False},
    "guidance.applied": {"text": "help", "source": "user", "truncated": False},
    "interaction.required": {"request": {"interaction_id": "i", "session_id": "session", "thread_id": "thread", "turn_id": "turn", "input_kind": "text", "purpose": "clarify", "prompt": "which?"}},
    "interaction.resolved": {"interaction_id": "i", "purpose": "clarify", "resolution": {"value": "yes", "free_text": True, "decision": "accept", "resolution_reason": "answered"}},
}


def wire(kind="tool.started", **changes):
    return {"schema_version": 1, "event_id": str(uuid4()), "session_id": "session", "thread_id": "thread", "turn_id": "turn", "sequence": 1, "timestamp": 1789257600.0, "agent_id": None, "parent_tool_call_id": None, "kind": kind, "payload": PAYLOADS.get(kind, {}), **changes}


@pytest.mark.parametrize("kind", PAYLOADS)
def test_all_kinds_roundtrip(kind):
    event = events.decode_event(json.dumps(wire(kind)).encode())
    assert events.decode_event(events.encode_event(event)) == event
    assert TypeAdapter(events.SemanticEvent).validate_python(event) == event
    if kind == "turn.started":
        assert isinstance(event.payload.metadata, TurnMetadata)


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("schema_version", True), ("sequence", 0), ("sequence", -1), ("sequence", True), ("sequence", 1.5), ("event_id", "bad"), ("session_id", ""), ("thread_id", " "), ("turn_id", ""), ("timestamp", float("inf")), ("timestamp", "2026-01-01"), ("kind", "unknown")])
def test_invalid_envelope(field, value):
    with pytest.raises(ValueError):
        events.decode_event(json.dumps(wire(**{field: value})).encode())


@pytest.mark.parametrize("field", ["session_id", "thread_id", "turn_id", "sequence", "event_id", "timestamp", "agent_id", "parent_tool_call_id", "payload"])
def test_required_envelope(field):
    data = wire()
    del data[field]
    with pytest.raises(ValueError):
        events.decode_event(json.dumps(data).encode())


@pytest.mark.parametrize("kind", PAYLOADS)
def test_business_payload_is_required(kind):
    with pytest.raises(ValueError):
        events.decode_event(json.dumps(wire(kind, payload={})).encode())


def test_limits_and_exact_boundary():
    config = events.SemanticEventConfig()
    assert config.max_event_bytes == 256 * 1024
    assert config.queue_capacity == 256
    event = events.decode_event(json.dumps(wire()).encode())
    encoded = events.encode_event(event)
    assert events.encode_event(event, max_event_bytes=len(encoded)) == encoded
    assert events.decode_event(encoded, max_event_bytes=len(encoded)) == event
    for func, arg in [(events.encode_event, event), (events.decode_event, encoded)]:
        with pytest.raises(events.EventSizeExceeded):
            func(arg, max_event_bytes=len(encoded) - 1)
    for value in [0, -1, True, 1.5]:
        for field in ["max_event_bytes", "queue_capacity"]:
            with pytest.raises(ValueError):
                events.SemanticEventConfig(**{field: value})
        with pytest.raises(ValueError):
            events.encode_event(event, max_event_bytes=value)


def test_snapshot_is_deeply_independent_and_revalidates_mutation():
    producer = events.decode_event(json.dumps(wire()).encode())
    snapshot = events.encode_event(producer)
    first, second = events.decode_event(snapshot), events.decode_event(snapshot)
    producer.payload.arguments["nested"][0]["a"] = 9
    first.payload.arguments["nested"].append(7)
    assert second.payload.arguments == {"nested": [{"a": 1}]}
    producer.payload.arguments["bad"] = object()
    with pytest.raises((ValueError, TypeError)):
        events.encode_event(producer)


@pytest.mark.parametrize("bad", [object(), {1: "value"}, {"x": (1, 2)}, {"x": {1, 2}}, {"x": b"bytes"}, {"x": float("nan")}, {"x": float("inf")}])
def test_non_json_arguments_rejected(bad):
    with pytest.raises((ValueError, TypeError)):
        events.ToolStarted(**wire(payload={**TOOL, "arguments": bad}))


@pytest.mark.parametrize("raw", [b'{"kind":"tool.started","kind":"turn.started"}', b'NaN', b'[]', b'null', b'\xff'])
def test_invalid_json_rejected(raw):
    with pytest.raises(ValueError):
        events.decode_event(raw)


def test_content_or_reference_is_mandatory():
    for kind, payload in [("assistant.committed", STREAM), ("file.changed", {"tool_call_id": "tc", "path": "a"})]:
        with pytest.raises(ValueError):
            events.decode_event(json.dumps(wire(kind, payload=payload)).encode())


def test_encoded_text_chunking_accounts_for_escapes_and_envelope():
    original = '你好😀"\\\n' * 30
    template = events.decode_event(json.dumps(wire("assistant.chunk", payload={**STREAM, "delta": original})).encode())
    chunks = list(events.chunk_text_event(template, max_event_bytes=360))
    assert len(chunks) > 1
    assert "".join(chunk.payload.delta for chunk in chunks) == original
    assert len({chunk.event_id for chunk in chunks}) == len(chunks)
    assert [chunk.sequence for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert all(len(events.encode_event(chunk)) <= 360 for chunk in chunks)
    with pytest.raises(events.EventSizeExceeded):
        list(events.chunk_text_event(template, max_event_bytes=10))


@pytest.mark.parametrize("field", ["session_id", "thread_id", "turn_id"])
def test_interaction_request_ownership_matches_envelope(field):
    data = wire("interaction.required", **{field: "other"})
    with pytest.raises(ValueError):
        events.decode_event(json.dumps(data).encode())


def test_interaction_reuses_domain_types():
    from voidx.tooling.domain.interaction import InteractionRequest, InteractionResolution
    required = events.decode_event(json.dumps(wire("interaction.required")).encode())
    resolved = events.decode_event(json.dumps(wire("interaction.resolved")).encode())
    assert isinstance(required.payload.request, InteractionRequest)
    assert isinstance(resolved.payload.resolution, InteractionResolution)


def test_interaction_any_fields_reject_non_json_instead_of_coercing():
    from voidx.tooling.domain.interaction import InteractionPermissionTool
    event = events.decode_event(json.dumps(wire("interaction.required")).encode())
    event.payload.request.tools.append(InteractionPermissionTool(name="read", args={"bad": (1, 2)}))
    with pytest.raises(ValueError):
        events.encode_event(event)
