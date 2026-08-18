"""Detached turns must not bind the host session.

Goal evaluator turns run with detached=True so they never load the work-phase
conversation history; they must also never write messages into the host
session. Regression test for goal-mode context pollution.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.adapters.langgraph.runtime import thread_context
from voidx.agent.adapters.langgraph.runtime.thread_context import (
    GuidanceEntry,
    _state_for_context,
)


class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id


@pytest.mark.asyncio
async def test_state_for_context_detached_returns_detached_state():
    """detached=True yields a detached state, never the host session."""
    host = SimpleNamespace(
        _session=_FakeSession("host-1"),
        _thread_execution_states={},
    )
    state = await _state_for_context(host, "host-1", detached=True)
    assert state.session is None
    assert state.session_msg_cache is None
    assert state.host_id == id(host)


@pytest.mark.asyncio
async def test_state_for_context_empty_session_id_falls_back_to_host():
    """Empty session_id without detached keeps the historical host fallback."""
    host = SimpleNamespace(
        _session=_FakeSession("host-1"),
        _thread_execution_states={},
    )
    state = await _state_for_context(host, "")
    assert state.session is not None
    assert state.session.id == "host-1"


@pytest.mark.asyncio
async def test_state_for_context_isolates_threads_within_one_session():
    host = SimpleNamespace(
        _session=_FakeSession("host-1"),
        _thread_execution_states={},
    )

    state_a = await _state_for_context(host, "host-1", thread_id="thread-a")
    state_a.pending_guidance.append(
        GuidanceEntry(
            text="only thread A",
            thread_id="thread-a",
            session_id="host-1",
        )
    )
    state_b = await _state_for_context(host, "host-1", thread_id="thread-b")

    assert state_b is not state_a
    assert state_b.pending_guidance == []
    assert [entry.text for entry in state_a.pending_guidance] == ["only thread A"]


@pytest.mark.asyncio
async def test_clear_thread_execution_states_only_removes_target_session():
    state_host = object()
    state_thread = object()
    state_other = object()
    state_similar = object()
    host = SimpleNamespace(
        _thread_execution_states={
            "host-1": state_host,
            "host-1\x1fthread-a": state_thread,
            "other": state_other,
            "host-10\x1fthread-a": state_similar,
        },
    )

    thread_context.clear_thread_execution_states(host, "host-1")

    assert host._thread_execution_states == {
        "other": state_other,
        "host-10\x1fthread-a": state_similar,
    }
