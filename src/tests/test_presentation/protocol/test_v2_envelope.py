"""Tests for protocol v2 JSON-RPC envelope parsing rules.

Covers the four message types and the field-existence parsing rules:
- id + method         → Request
- method + no id      → Notification
- id + result         → Result
- id + error          → Error
- id + no method/result/error → invalid (-32600)
"""

from __future__ import annotations

import pytest

from voidx.presentation.protocol.v2.envelope import (
    ErrorPayload,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
    PROTOCOL_VERSION,
    parse_jsonrpc_message,
    ParseError,
)


# ── message construction ────────────────────────────────────────────────


def test_request_has_id_and_method():
    msg = JsonRpcRequest(id=1, method="session.create", params={"title": "x"})
    assert msg.jsonrpc == PROTOCOL_VERSION
    assert msg.id == 1
    assert msg.method == "session.create"
    assert msg.params == {"title": "x"}


def test_notification_has_no_id():
    msg = JsonRpcNotification(method="item.delta", params={"item_id": "i1"})
    assert msg.jsonrpc == PROTOCOL_VERSION
    assert not hasattr(msg, "id") or "id" not in msg.model_fields


def test_result_has_id_and_result():
    msg = JsonRpcResult(id=2, result={"ok": True})
    assert msg.id == 2
    assert msg.result == {"ok": True}


def test_error_has_id_and_error_payload():
    msg = JsonRpcError(
        id=3,
        error=ErrorPayload(code=-32601, message="method not found"),
    )
    assert msg.id == 3
    assert msg.error.code == -32601
    assert msg.error.message == "method not found"


def test_error_id_can_be_none_for_parse_error():
    msg = JsonRpcError(
        id=None,
        error=ErrorPayload(code=-32700, message="parse error"),
    )
    assert msg.id is None


# ── parsing rules ───────────────────────────────────────────────────────


def test_parse_request():
    raw = {"jsonrpc": "2.0", "id": 1, "method": "session.create", "params": {}}
    msg = parse_jsonrpc_message(raw)
    assert isinstance(msg, JsonRpcRequest)
    assert msg.method == "session.create"


def test_parse_notification():
    raw = {"jsonrpc": "2.0", "method": "item.delta", "params": {"item_id": "i1"}}
    msg = parse_jsonrpc_message(raw)
    assert isinstance(msg, JsonRpcNotification)
    assert msg.method == "item.delta"


def test_parse_result():
    raw = {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}
    msg = parse_jsonrpc_message(raw)
    assert isinstance(msg, JsonRpcResult)
    assert msg.result == {"ok": True}


def test_parse_error():
    raw = {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32601, "message": "method not found"},
    }
    msg = parse_jsonrpc_message(raw)
    assert isinstance(msg, JsonRpcError)
    assert msg.error.code == -32601


def test_parse_request_without_params_defaults_to_empty():
    raw = {"jsonrpc": "2.0", "id": 5, "method": "session.list"}
    msg = parse_jsonrpc_message(raw)
    assert isinstance(msg, JsonRpcRequest)
    assert msg.params == {}


def test_parse_notification_without_params_defaults_to_empty():
    raw = {"jsonrpc": "2.0", "method": "capture.started"}
    msg = parse_jsonrpc_message(raw)
    assert isinstance(msg, JsonRpcNotification)
    assert msg.params == {}


# ── invalid messages ────────────────────────────────────────────────────


def test_parse_invalid_id_without_method_result_error_raises():
    raw = {"jsonrpc": "2.0", "id": 9}
    with pytest.raises(ParseError) as exc_info:
        parse_jsonrpc_message(raw)
    assert exc_info.value.code == -32600


def test_parse_missing_jsonrpc_field_raises():
    raw = {"id": 1, "method": "session.create"}
    with pytest.raises(ParseError) as exc_info:
        parse_jsonrpc_message(raw)
    assert exc_info.value.code == -32600


def test_parse_wrong_jsonrpc_version_raises():
    raw = {"jsonrpc": "1.0", "id": 1, "method": "session.create"}
    with pytest.raises(ParseError) as exc_info:
        parse_jsonrpc_message(raw)
    assert exc_info.value.code == -32600


def test_parse_result_with_method_raises():
    """A message cannot be both a request and a result."""
    raw = {"jsonrpc": "2.0", "id": 1, "method": "x", "result": {}}
    with pytest.raises(ParseError) as exc_info:
        parse_jsonrpc_message(raw)
    assert exc_info.value.code == -32600


def test_parse_error_without_error_object_raises():
    raw = {"jsonrpc": "2.0", "id": 1, "error": "not an object"}
    with pytest.raises(ParseError) as exc_info:
        parse_jsonrpc_message(raw)
    assert exc_info.value.code == -32600


# ── serialization round-trip ────────────────────────────────────────────


def test_request_round_trip():
    msg = JsonRpcRequest(id=10, method="agent.submit", params={"text": "hi"})
    raw = msg.model_dump()
    parsed = parse_jsonrpc_message(raw)
    assert isinstance(parsed, JsonRpcRequest)
    assert parsed.method == "agent.submit"
    assert parsed.params == {"text": "hi"}


def test_notification_round_trip():
    msg = JsonRpcNotification(method="turn.started", params={"turn_id": "t1"})
    raw = msg.model_dump()
    parsed = parse_jsonrpc_message(raw)
    assert isinstance(parsed, JsonRpcNotification)
    assert parsed.method == "turn.started"
