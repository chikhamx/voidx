from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from voidx.agent.domain.goal import GOAL_PROFILE, GoalSpec, GoalState
from voidx.agent.domain.thread import AgentThread, LifecycleState
from voidx.agent.goal.runner import GoalRuntimeRunner
from voidx.agent.runtime.contracts import TurnResult


class Runtime:
    def __init__(self) -> None:
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        return TurnResult(
            thread=request.thread,
            lifecycle=LifecycleState.COMPLETED,
            final_llm_messages=(AIMessage(content="work completed"),),
            final_assistant_summary="work completed",
        )


class Evaluator:
    async def run_phase(self, *, runtime, thread, context, prompt, controller, work_result):
        del runtime, thread, context, prompt, work_result
        await controller.submit_decision({
            "outcome": "completed",
            "summary": "acceptance verified",
            "reason": "verified",
            "progress": "meaningful",
        })


def _thread(spec: GoalSpec):
    state = GoalState.from_spec(spec, run_id=spec.generation)
    thread = AgentThread(
        thread_id=spec.goal_thread_id("parent"),
        session_id=spec.goal_session_id("parent"),
        workspace="/tmp/ws",
        lifecycle=LifecycleState.READY,
    )
    return thread, state


@pytest.mark.asyncio
async def test_goal_runner_uses_evaluator_controller_decision() -> None:
    spec = GoalSpec(objective="ship", acceptance_condition="tests pass", generation="run-1")
    thread, state = _thread(spec)
    runtime = Runtime()

    decision = await GoalRuntimeRunner(runtime=runtime, evaluator=Evaluator()).run_turn(
        thread=thread,
        profile=None,
        input_frame={"spec": spec.model_dump(mode="json"), "goal_state": state.model_dump(mode="json")},
    )

    assert decision.outcome == "completed"
    assert decision.reason == "verified"
    assert decision.metadata.goal_state_patch.attempt_count == 1
    assert len(runtime.requests) == 1


@pytest.mark.asyncio
async def test_goal_runner_fails_closed_when_evaluator_submits_nothing() -> None:
    class MissingEvaluator:
        async def run_phase(self, **_kwargs):
            return None

    spec = GoalSpec(objective="ship", acceptance_condition="tests pass", generation="run-1")
    thread, state = _thread(spec)
    decision = await GoalRuntimeRunner(runtime=Runtime(), evaluator=MissingEvaluator()).run_turn(
        thread=thread,
        profile=None,
        input_frame={"spec": spec.model_dump(mode="json"), "goal_state": state.model_dump(mode="json")},
    )

    assert decision.outcome == "blocked"
    assert decision.reason == "missing_goal_decision"


@pytest.mark.asyncio
async def test_goal_runner_blocks_before_work_when_attempt_limit_reached() -> None:
    from voidx.agent.domain.goal import GoalSpec, GoalState
    from voidx.agent.domain.thread import AgentThread
    from voidx.agent.goal.runner import GoalRuntimeRunner

    class Runtime:
        calls = 0
        async def run_turn(self, request):
            self.calls += 1

    runtime = Runtime()
    spec = GoalSpec(objective="x", acceptance_condition="y", max_attempts=1)
    state = GoalState.from_spec(spec, run_id="run")
    state = state.model_copy(update={"attempt_count": 1})
    result = await GoalRuntimeRunner(runtime=runtime, evaluator=object()).run_turn(
        thread=AgentThread(thread_id="goal:p:r", session_id="goal:p:r"),
        profile=GOAL_PROFILE,
        input_frame={"spec": spec.model_dump(), "goal_state": state.model_dump()},
    )

    assert result.outcome == "blocked"
    assert result.reason == "max_attempts_exceeded"
    assert runtime.calls == 0
