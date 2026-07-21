from __future__ import annotations

import pytest
from pydantic import ValidationError

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import AgentThread, LifecycleState
from voidx.agent.domain.turn import TurnExecution
from voidx.agent.runtime.contracts import TurnRequest, TurnResult


def test_runtime_profile_is_immutable_and_versioned():
    profile = RuntimeProfile(profile_id="coding", revision=1, name="Coding")

    assert profile.profile_id == "coding"
    with pytest.raises(ValidationError):
        profile.profile_id = "chat"




def test_turn_execution_carries_resolved_identity():
    execution = TurnExecution(thread_id="thread-1", session_id="session-1")

    assert execution.thread_id == "thread-1"
    assert execution.session_id == "session-1"
def test_agent_thread_allows_lazy_session_identity():
    thread = AgentThread(thread_id="thread-1")

    assert thread.session_id is None
    assert thread.lifecycle is LifecycleState.CREATED


def test_turn_request_and_result_carry_final_session_identity():
    request = TurnRequest(thread=AgentThread(thread_id="thread-1"), user_text="hello")
    result = TurnResult(
        thread=request.thread.model_copy(update={"session_id": "session-1"}),
        lifecycle=LifecycleState.COMPLETED,
    )

    assert result.session_id == "session-1"
    assert result.lifecycle is LifecycleState.COMPLETED
