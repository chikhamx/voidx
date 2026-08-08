"""Detached turns must not bind the host session.

Goal evaluator turns run with detached=True so they never load the work-phase
conversation history; they must also never write messages into the host
session. Regression test for goal-mode context pollution.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.adapters.langgraph.runtime.thread_context import _state_for_context


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
