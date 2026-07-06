"""Tests for protocol v2 Thread/Turn/Item primitives."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from voidx.ui.protocol.v2.threads import Item, ThreadInfo, TurnInfo


# ── ThreadInfo ──────────────────────────────────────────────────────────


def test_thread_info_defaults():
    info = ThreadInfo(thread_id="t1")
    assert info.thread_id == "t1"
    assert info.title == ""
    assert info.workspace == "."
    assert info.status == "idle"
    assert info.message_count == 0


def test_thread_info_status_values():
    for status in ("idle", "running", "waiting_for_user", "waiting_for_write_lock", "cancelling", "failed"):
        info = ThreadInfo(thread_id="t1", status=status)  # type: ignore[arg-type]
        assert info.status == status
    with pytest.raises(ValidationError):
        ThreadInfo(thread_id="t1", status="active")  # type: ignore[arg-type]


# ── TurnInfo ────────────────────────────────────────────────────────────


def test_turn_info_defaults():
    turn = TurnInfo(turn_id="turn1", thread_id="t1")
    assert turn.turn_id == "turn1"
    assert turn.thread_id == "t1"
    assert turn.status == "running"
    assert turn.elapsed is None


def test_turn_info_status_values():
    for status in ("running", "completed", "cancelled", "failed"):
        turn = TurnInfo(turn_id="turn1", thread_id="t1", status=status)  # type: ignore[arg-type]
        assert turn.status == status


# ── Item ────────────────────────────────────────────────────────────────


def test_item_defaults():
    item = Item(item_id="i1", turn_id="turn1", thread_id="t1", kind="message")
    assert item.lifecycle == "started"
    assert item.data == {}


def test_item_kind_values():
    for kind in ("message", "assistant_stream", "tool", "todo", "subagent", "status", "prompt"):
        item = Item(item_id="i1", turn_id="turn1", thread_id="t1", kind=kind)  # type: ignore[arg-type]
        assert item.kind == kind


def test_item_lifecycle_values():
    for lc in ("started", "delta", "completed"):
        item = Item(
            item_id="i1", turn_id="turn1", thread_id="t1",
            kind="tool", lifecycle=lc,  # type: ignore[arg-type]
        )
        assert item.lifecycle == lc


def test_item_data_carries_kind_specific_payload():
    item = Item(
        item_id="i1", turn_id="turn1", thread_id="t1",
        kind="tool", lifecycle="started",
        data={"tool_call_id": "tc1", "label": "read file"},
    )
    assert item.data["tool_call_id"] == "tc1"
    assert item.data["label"] == "read file"


def test_item_invalid_kind_rejected():
    with pytest.raises(ValidationError):
        Item(item_id="i1", turn_id="turn1", thread_id="t1", kind="unknown")  # type: ignore[arg-type]
