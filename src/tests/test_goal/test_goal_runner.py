from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from voidx.agent.application.agent_registry import AgentRegistry
from voidx.agent.application.automation.goal.runner import GoalRuntimeRunner
from voidx.agent.application.runtime.contracts import GoalPhaseResult, TurnResult
from voidx.agent.domain.automation.goal import GoalDecision, GoalSpec, GoalState, WorkCheckpoint
from voidx.agent.domain.thread import AgentThread, LifecycleState


class Runtime:
    def __init__(self, *, submit_checkpoint: bool = True, submit_decision: bool = True) -> None:
        self.requests = []
        self.submit_checkpoint = submit_checkpoint
        self.submit_decision = submit_decision

    async def run_turn(self, request):
        self.requests.append(request)
        if request.context.goal_phase == "work" and self.submit_checkpoint:
            await request.context.goal_checkpoint_controller.submit_checkpoint(
                WorkCheckpoint(
                    generation=request.context.goal_generation,
                    attempt_number=request.context.goal_attempt_number,
                    summary="work completed",
                    work_turn_id=request.context.goal_turn_id,
                ),
                protocol_id="checkpoint-protocol",
            )
        if request.context.goal_phase == "evaluator" and self.submit_decision:
            await request.context.goal_controller.submit_decision(
                {
                    "outcome": "completed",
                    "summary": "acceptance verified",
                    "reason": "verified",
                    "progress": "meaningful",
                },
                protocol_id="decision-protocol",
            )
        return TurnResult(
            thread=request.thread,
            lifecycle=LifecycleState.COMPLETED,
            final_llm_messages=(AIMessage(content="phase completed"),),
            final_assistant_summary="phase completed",
        )


def _goal_profile():
    return AgentRegistry("/tmp/ws").resolve("goal")


def _thread(spec: GoalSpec):
    state = GoalState.from_spec(
        spec,
        run_id=spec.generation,
        main_session_id="main-session",
        work_session_id="work-session",
        evaluator_session_id="evaluator-session",
    )
    thread = AgentThread(
        thread_id=spec.goal_thread_id("parent"),
        session_id="work-session",
        workspace="/tmp/ws",
        lifecycle=LifecycleState.READY,
    )
    return thread, state


def _frame(spec: GoalSpec, state: GoalState, *, phase: str, checkpoint: WorkCheckpoint | None = None):
    frame = {
        "phase": phase,
        "attempt_number": state.attempt_count + 1,
        "spec": spec.model_dump(mode="json"),
        "goal_state": state.model_dump(mode="json"),
        "attempt_id": f"attempt-{phase}",
        "lease_owner": "worker-a",
        "fencing_token": 7,
    }
    if checkpoint is not None:
        frame["checkpoint"] = checkpoint.model_dump(mode="json")
    return frame


@pytest.mark.asyncio
async def test_work_outbox_runs_only_work_and_returns_checkpoint_protocol() -> None:
    spec = GoalSpec(objective="ship", acceptance_condition="tests pass", generation="run-work")
    thread, state = _thread(spec)
    runtime = Runtime()

    result = await GoalRuntimeRunner(runtime=runtime).run_turn(
        thread=thread,
        profile=_goal_profile(),
        input_frame=_frame(spec, state, phase="work"),
    )

    assert result == GoalPhaseResult(
        phase="work",
        attempt_number=1,
        protocol_id="checkpoint-protocol",
    )
    assert [request.context.goal_phase for request in runtime.requests] == ["work"]
    assert runtime.requests[0].thread.session_id == "work-session"


@pytest.mark.asyncio
async def test_evaluator_outbox_skips_work_and_consumes_durable_checkpoint() -> None:
    spec = GoalSpec(objective="ship", acceptance_condition="tests pass", generation="run-evaluator")
    thread, state = _thread(spec)
    state = state.model_copy(update={"current_phase": "evaluator"})
    checkpoint = WorkCheckpoint(
        generation=spec.generation,
        attempt_number=1,
        summary="durable checkpoint summary",
        evidence=("tests passed",),
        work_turn_id="work-turn-1",
    )
    runtime = Runtime()

    result = await GoalRuntimeRunner(runtime=runtime).run_turn(
        thread=thread,
        profile=_goal_profile(),
        input_frame=_frame(spec, state, phase="evaluator", checkpoint=checkpoint),
    )

    assert result == GoalPhaseResult(
        phase="evaluator",
        attempt_number=1,
        protocol_id="decision-protocol",
    )
    assert [request.context.goal_phase for request in runtime.requests] == ["evaluator"]
    request = runtime.requests[0]
    assert request.thread.session_id == "evaluator-session"
    assert "durable checkpoint summary" in request.user_text
    assert "tests passed" in request.user_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "runtime", "reason"),
    [
        ("work", Runtime(submit_checkpoint=False), "missing_work_checkpoint"),
        ("evaluator", Runtime(submit_decision=False), "missing_goal_decision"),
    ],
)
async def test_missing_phase_protocol_returns_needs_resume_without_lifecycle_decision(
    phase: str,
    runtime: Runtime,
    reason: str,
) -> None:
    spec = GoalSpec(objective="ship", acceptance_condition="tests pass", generation=f"run-{phase}-missing")
    thread, state = _thread(spec)
    checkpoint = None
    if phase == "evaluator":
        state = state.model_copy(update={"current_phase": "evaluator"})
        checkpoint = WorkCheckpoint(
            generation=spec.generation,
            attempt_number=1,
            summary="work done",
            work_turn_id="work-turn",
        )

    result = await GoalRuntimeRunner(runtime=runtime).run_turn(
        thread=thread,
        profile=_goal_profile(),
        input_frame=_frame(spec, state, phase=phase, checkpoint=checkpoint),
    )

    assert result.phase == phase
    assert result.protocol_id == ""
    assert result.needs_resume is True
    assert result.reason == reason


@pytest.mark.asyncio
async def test_attempt_limit_never_synthesizes_blocked_without_decision_record() -> None:
    runtime = Runtime()
    spec = GoalSpec(objective="x", acceptance_condition="y", max_attempts=1, generation="run-limit")
    thread, state = _thread(spec)
    state = state.model_copy(update={"attempt_count": 1})

    result = await GoalRuntimeRunner(runtime=runtime).run_turn(
        thread=thread,
        profile=_goal_profile(),
        input_frame=_frame(spec, state, phase="work"),
    )

    assert result.needs_resume is True
    assert result.reason == "max_attempts_exceeded"
    assert result.protocol_id == ""
    assert runtime.requests == []


@pytest.mark.asyncio
async def test_goal_runner_forwards_matching_guidance_snapshot_to_phase_turn() -> None:
    spec = GoalSpec(objective="ship", acceptance_condition="tests pass", generation="run-guidance")
    thread, state = _thread(spec)
    runtime = Runtime()
    snapshot = {
        "guidance_id": "guidance-1",
        "text": "keep the attempt narrow",
        "source": "user",
        "target_phase": "work",
    }
    frame = _frame(spec, state, phase="work")
    frame["guidance"] = [snapshot]

    await GoalRuntimeRunner(runtime=runtime).run_turn(
        thread=thread,
        profile=_goal_profile(),
        input_frame=frame,
    )

    assert runtime.requests[0].guidance == (snapshot,)


def test_goal_decision_fixture_matches_evaluator_protocol_shape() -> None:
    decision = GoalDecision(
        generation="run",
        attempt_number=1,
        status="finished",
        summary="accepted",
    )
    assert decision.status == "finished"


@pytest.mark.asyncio
async def test_work_uses_current_turn_tool_observations_for_durable_fallback() -> None:
    class FallbackRuntime(Runtime):
        async def run_turn(self, request):
            result = await super().run_turn(request)
            return result.model_copy(
                update={
                    "current_turn_tool_result_summaries": (
                        "read: observed current workspace state",
                    )
                }
            )

    class RecordingStore:
        def __init__(self) -> None:
            self.records = []

        async def submit_goal_protocol(self, record, **kwargs):
            self.records.append((record, kwargs))
            return record

    spec = GoalSpec(
        objective="ship",
        acceptance_condition="tests pass",
        generation="run-fallback",
    )
    thread, state = _thread(spec)
    runtime = FallbackRuntime(submit_checkpoint=False)
    store = RecordingStore()

    result = await GoalRuntimeRunner(runtime=runtime, store=store).run_turn(
        thread=thread,
        profile=_goal_profile(),
        input_frame=_frame(spec, state, phase="work"),
    )

    assert result.protocol_id == "goal-fallback-attempt-work"
    assert result.needs_resume is False
    assert len(store.records) == 1
    record, fencing = store.records[0]
    checkpoint = record.payload_model()
    assert checkpoint.source == "runtime_fallback"
    assert checkpoint.completeness == "incomplete"
    assert checkpoint.progress == "none"
    assert checkpoint.evidence == ()
    assert checkpoint.changed_files == ()
    assert checkpoint.verification == ()
    assert checkpoint.observed_tool_result_summaries == (
        "read: observed current workspace state",
    )
    assert fencing == {
        "attempt_id": "attempt-work",
        "lease_owner": "worker-a",
        "fencing_token": 7,
    }
