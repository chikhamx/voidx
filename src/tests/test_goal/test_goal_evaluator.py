from __future__ import annotations

import pytest

from voidx.agent.application.automation.goal.evaluator import GoalEvaluator
from voidx.agent.domain.automation.goal import GOAL_PROFILE, WorkCheckpoint
from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext




def _checkpoint() -> WorkCheckpoint:
    return WorkCheckpoint(
        generation="run",
        attempt_number=1,
        summary="reorganized src/tests into 25 test dirs",
        evidence=("4073 tests passed",),
        changed_files=("src/tests",),
        verification=("backend suite passed",),
        work_turn_id="work-turn-1",
    )


def _context(*, evaluator_session_id: str = "evaluator-session") -> TurnExecutionContext:
    return TurnExecutionContext(
        thread_id="goal:t:run",
        session_id="work-session",
        goal_evaluator_session_id=evaluator_session_id,
        runtime_profile=GOAL_PROFILE,
        goal_phase="evaluator",
    )


@pytest.mark.asyncio
async def test_goal_evaluator_runs_tool_capable_phase() -> None:
    thread = AgentThread(thread_id="goal:t:run", session_id="work-session")

    request = GoalEvaluator().build_request(
        thread=thread,
        context=_context(),
        prompt="verify",
        checkpoint=_checkpoint(),
    )

    assert request.context.goal_phase == "evaluator"
    assert request.persist_user_input is False


@pytest.mark.asyncio
async def test_goal_evaluator_runs_with_independent_context() -> None:
    thread = AgentThread(thread_id="goal:t:run", session_id="work-session")

    request = GoalEvaluator().build_request(
        thread=thread,
        context=_context(),
        prompt="verify",
        checkpoint=_checkpoint(),
    )
    assert request.thread.session_id == "evaluator-session"
    assert request.thread.thread_id != thread.thread_id
    assert request.context.session_id == "evaluator-session"
    assert request.context.detached is False


@pytest.mark.asyncio
async def test_goal_evaluator_prompt_uses_only_structured_checkpoint() -> None:
    thread = AgentThread(thread_id="goal:t:run", session_id="work-session")

    request = GoalEvaluator().build_request(
        thread=thread,
        context=_context(),
        prompt="verify",
        checkpoint=_checkpoint(),
    )

    prompt = request.user_text
    assert "reorganized src/tests into 25 test dirs" in prompt
    assert "4073 tests passed" in prompt
    assert "backend suite passed" in prompt
    assert "src/tests" in prompt


@pytest.mark.asyncio
async def test_goal_evaluator_rejects_missing_durable_session_binding() -> None:
    thread = AgentThread(thread_id="goal:t:run", session_id="work-session")

    with pytest.raises(ValueError, match="Goal evaluator session binding is missing"):
        GoalEvaluator().build_request(
            thread=thread,
            context=_context(evaluator_session_id=""),
            prompt="verify",
            checkpoint=_checkpoint(),
        )
