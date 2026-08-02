from __future__ import annotations

import pytest

from voidx.agent.domain.goal import GOAL_PROFILE
from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.goal.evaluator import GoalEvaluator


class Runtime:
    def __init__(self):
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)


@pytest.mark.asyncio
async def test_goal_evaluator_runs_tool_capable_phase() -> None:
    runtime = Runtime()
    thread = AgentThread(thread_id="goal:t:run", session_id="goal:t:run")
    context = TurnExecutionContext(
        thread_id=thread.thread_id,
        session_id=thread.session_id or "",
        runtime_profile=GOAL_PROFILE,
        goal_phase="evaluator",
    )

    await GoalEvaluator().run_phase(
        runtime=runtime,
        thread=thread,
        context=context,
        prompt="verify",
        controller=object(),
        work_result=object(),
    )

    assert runtime.requests[0].context.goal_phase == "evaluator"
    assert runtime.requests[0].persist_user_input is False


@pytest.mark.asyncio
async def test_goal_evaluator_runs_with_independent_context() -> None:
    """Evaluator turn must not load the work-phase session history."""
    runtime = Runtime()
    thread = AgentThread(thread_id="goal:t:run", session_id="goal:t:run")
    context = TurnExecutionContext(
        thread_id=thread.thread_id,
        session_id=thread.session_id or "",
        runtime_profile=GOAL_PROFILE,
        goal_phase="evaluator",
    )

    await GoalEvaluator().run_phase(
        runtime=runtime,
        thread=thread,
        context=context,
        prompt="verify",
        controller=object(),
        work_result=None,
    )

    request = runtime.requests[0]
    assert request.thread.session_id is None
    assert request.thread.thread_id != thread.thread_id
    assert request.context.session_id == ""


@pytest.mark.asyncio
async def test_goal_evaluator_prompt_includes_work_evidence() -> None:
    from voidx.agent.domain.thread import LifecycleState
    from voidx.agent.runtime.contracts import TurnResult

    runtime = Runtime()
    thread = AgentThread(thread_id="goal:t:run", session_id="goal:t:run")
    context = TurnExecutionContext(
        thread_id=thread.thread_id,
        session_id=thread.session_id or "",
        runtime_profile=GOAL_PROFILE,
        goal_phase="evaluator",
    )
    work_result = TurnResult(
        thread=thread,
        lifecycle=LifecycleState.COMPLETED,
        final_assistant_summary="reorganized src/tests into 25 test_ dirs",
        tool_result_summaries=("bash: 4073 passed, 0 failed", "find: 25 dirs"),
    )

    await GoalEvaluator().run_phase(
        runtime=runtime,
        thread=thread,
        context=context,
        prompt="verify",
        controller=object(),
        work_result=work_result,
    )

    prompt = runtime.requests[0].user_text
    assert "reorganized src/tests into 25 test_ dirs" in prompt
    assert "bash: 4073 passed, 0 failed" in prompt
    assert "find: 25 dirs" in prompt
