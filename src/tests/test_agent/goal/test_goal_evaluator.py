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
