from __future__ import annotations

import pytest

from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.domain.agent_profile import AgentProfileSnapshot
from voidx.agent.domain.automation.goal import (
    GOAL_PROFILE,
    GoalProtocolRecord,
    GoalSpec,
    GoalSpecSnapshot,
    GoalState,
    WorkCheckpoint,
)
from voidx.agent.domain.guidance import Guidance
from voidx.agent.domain.thread import AgentThreadState, LifecycleState
from voidx.agent.ports.persistence import GoalProtocolConflict


@pytest.fixture
def store(tmp_path) -> ThreadStore:
    return ThreadStore(tmp_path / "store.db")


def _profile_snapshot() -> AgentProfileSnapshot:
    return AgentProfileSnapshot(
        profile_id="goal",
        revision=1,
        source="bundled",
        content_hash="content",
        snapshot_hash="snapshot",
        canonical_payload={"profile_id": "goal"},
    )


async def _initialize_attempt(store: ThreadStore):
    spec = GoalSpec(
        objective="ship feature",
        acceptance_condition="tests pass",
        generation="gen-phase-commit",
    )
    state = GoalState.from_spec(
        spec,
        run_id=spec.generation,
        main_session_id="main-phase-commit",
        work_session_id="work-phase-commit",
        evaluator_session_id="eval-phase-commit",
    )
    await store.ensure_session(
        "main-phase-commit",
        "/workspace",
        profile="goal",
        profile_snapshot=_profile_snapshot(),
    )
    await store.initialize_goal_generation(
        generation=spec.generation,
        main_session_id="main-phase-commit",
        evaluator_session_id="eval-phase-commit",
        work_session_id="work-phase-commit",
        goal_thread_id="goal:main-phase-commit:gen-phase-commit",
        parent_thread_id="main-phase-commit",
        workspace="/workspace",
        profile_id="goal",
        profile_snapshot=_profile_snapshot(),
        thread_profile=GOAL_PROFILE,
        thread_state=AgentThreadState(
            thread_id="goal:main-phase-commit:gen-phase-commit",
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
        protocol=GoalProtocolRecord.submitted(
            protocol_id="protocol-init-phase-commit",
            parent_session_id="main-phase-commit",
            generation=spec.generation,
            phase="init",
            attempt_number=0,
            turn_id="turn-init-phase-commit",
            session_id="main-phase-commit",
            payload=GoalSpecSnapshot.from_spec(
                spec,
                parent_session_id="main-phase-commit",
                profile_snapshot={"profile_id": "goal"},
            ),
        ),
    )
    outbox = (await store.list_pending_outbox("goal:main-phase-commit:gen-phase-commit"))[0]
    claimed = await store.claim_outbox(
        outbox.outbox_id,
        lease_owner="worker-current",
        lease_seconds=60,
    )
    assert claimed is not None
    attempt = await store.begin_attempt(
        thread_id=outbox.thread_id,
        source_outbox_id=outbox.outbox_id,
        input_frame=outbox.payload,
        expected_state_version=outbox.expected_state_version,
        lease_owner="worker-current",
        lease_seconds=60,
    )
    attempt = await store.mark_side_effect_started(
        attempt.attempt_id,
        lease_owner="worker-current",
        fencing_token=attempt.fencing_token,
    )
    assert attempt is not None
    record = GoalProtocolRecord.submitted(
        protocol_id="protocol-checkpoint-phase-commit",
        parent_session_id="main-phase-commit",
        generation=spec.generation,
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn-work-phase-commit",
        session_id="work-phase-commit",
        payload=WorkCheckpoint(
            generation=spec.generation,
            attempt_number=1,
            summary="implementation complete",
            work_turn_id="turn-work-phase-commit",
        ),
    )
    return outbox, attempt, record


@pytest.mark.asyncio
async def test_stale_attempt_cannot_submit_goal_protocol(store: ThreadStore) -> None:
    _, attempt, record = await _initialize_attempt(store)

    with pytest.raises(GoalProtocolConflict, match="attempt lease"):
        await store.submit_goal_protocol(
            record,
            attempt_id=attempt.attempt_id,
            lease_owner="worker-stale",
            fencing_token=attempt.fencing_token - 1,
        )

    assert await store.get_goal_protocol(record.protocol_id) is None


@pytest.mark.asyncio
async def test_goal_phase_commit_projects_and_closes_attempt_atomically(store: ThreadStore) -> None:
    outbox, attempt, record = await _initialize_attempt(store)
    delivery_id = f"attempt:{outbox.outbox_id}"
    await store.submit_guidance(
        Guidance(
            guidance_id="guidance-phase-commit",
            text="verify the public path",
            target_run_id="gen-phase-commit",
            target_phase="work",
        )
    )
    assert await store.bind_guidance(
        delivery_id,
        run_id="gen-phase-commit",
        phase="work",
    )
    submitted = await store.submit_goal_protocol(
        record,
        attempt_id=attempt.attempt_id,
        lease_owner="worker-current",
        fencing_token=attempt.fencing_token,
    )

    projected = await store.commit_goal_phase(
        attempt_id=attempt.attempt_id,
        protocol_id=submitted.protocol_id,
        lease_owner="worker-current",
        fencing_token=attempt.fencing_token,
        guidance_delivery_id=delivery_id,
    )

    assert projected.status == "projected"
    committed_attempt = await store.get_attempt(attempt.attempt_id)
    assert committed_attempt is not None
    assert committed_attempt.status == "committed"
    guidance = await store.get_guidance("guidance-phase-commit")
    assert guidance is not None and guidance.consumed_at is not None
    pending = await store.list_pending_outbox(outbox.thread_id)
    assert len(pending) == 1
    assert pending[0].payload["phase"] == "evaluator"
    loaded = await store.load(outbox.thread_id)
    assert loaded is not None
    goal_state = GoalState.model_validate(loaded.state.context["goal_run"])
    assert goal_state.projected_sequence_number == 1
    assert goal_state.current_phase == "evaluator"


@pytest.mark.asyncio
async def test_dispatcher_routes_goal_phase_without_generic_lifecycle_commit(
    store: ThreadStore,
    monkeypatch,
) -> None:
    from voidx.agent.application.runtime.contracts import GoalPhaseResult
    from voidx.agent.application.runtime.dispatcher import RuntimeDispatcher

    spec = GoalSpec(
        objective="ship feature",
        acceptance_condition="tests pass",
        generation="gen-dispatch-phase",
    )
    state = GoalState.from_spec(
        spec,
        run_id=spec.generation,
        main_session_id="main-dispatch-phase",
        work_session_id="work-dispatch-phase",
        evaluator_session_id="eval-dispatch-phase",
    )
    await store.ensure_session(
        "main-dispatch-phase",
        "/workspace",
        profile="goal",
        profile_snapshot=_profile_snapshot(),
    )
    await store.initialize_goal_generation(
        generation=spec.generation,
        main_session_id="main-dispatch-phase",
        evaluator_session_id="eval-dispatch-phase",
        work_session_id="work-dispatch-phase",
        goal_thread_id="goal:main-dispatch-phase:gen-dispatch-phase",
        parent_thread_id="main-dispatch-phase",
        workspace="/workspace",
        profile_id="goal",
        profile_snapshot=_profile_snapshot(),
        thread_profile=GOAL_PROFILE,
        thread_state=AgentThreadState(
            thread_id="goal:main-dispatch-phase:gen-dispatch-phase",
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
        protocol=GoalProtocolRecord.submitted(
            protocol_id="protocol-init-dispatch-phase",
            parent_session_id="main-dispatch-phase",
            generation=spec.generation,
            phase="init",
            attempt_number=0,
            turn_id="turn-init-dispatch-phase",
            session_id="main-dispatch-phase",
            payload=GoalSpecSnapshot.from_spec(
                spec,
                parent_session_id="main-dispatch-phase",
                profile_snapshot={"profile_id": "goal"},
            ),
        ),
    )

    async def generic_commit_must_not_run(**_kwargs):
        raise AssertionError("Goal phase used generic commit_decision")

    monkeypatch.setattr(store, "commit_decision", generic_commit_must_not_run)

    class Runner:
        seen_frame: dict = {}

        async def run_turn(self, *, thread, profile, input_frame):
            del thread, profile
            self.seen_frame = dict(input_frame)
            record = GoalProtocolRecord.submitted(
                protocol_id="protocol-checkpoint-dispatch-phase",
                parent_session_id="main-dispatch-phase",
                generation=spec.generation,
                phase="checkpoint",
                attempt_number=1,
                turn_id="turn-work-dispatch-phase",
                session_id="work-dispatch-phase",
                payload=WorkCheckpoint(
                    generation=spec.generation,
                    attempt_number=1,
                    summary="implementation complete",
                    work_turn_id="turn-work-dispatch-phase",
                ),
            )
            await store.submit_goal_protocol(
                record,
                attempt_id=input_frame["attempt_id"],
                lease_owner=input_frame["lease_owner"],
                fencing_token=input_frame["fencing_token"],
            )
            return GoalPhaseResult(
                phase="work",
                attempt_number=1,
                protocol_id=record.protocol_id,
            )

    runner = Runner()
    dispatcher = RuntimeDispatcher(
        store=store,
        runner=runner,
        lease_owner="worker-dispatch",
    )

    result = await dispatcher.dispatch_once()

    assert result is not None
    assert result.decision is None
    assert result.goal_phase is not None
    assert result.goal_phase.protocol_id == "protocol-checkpoint-dispatch-phase"
    assert runner.seen_frame["attempt_id"] == result.attempt_id
    assert runner.seen_frame["lease_owner"] == "worker-dispatch"
    assert runner.seen_frame["fencing_token"] > 0
    loaded = await store.load("goal:main-dispatch-phase:gen-dispatch-phase")
    assert loaded is not None
    goal_state = GoalState.model_validate(loaded.state.context["goal_run"])
    assert goal_state.current_phase == "evaluator"
    assert goal_state.projected_sequence_number == 1


@pytest.mark.asyncio
async def test_dispatcher_durably_fails_goal_transcript_corruption(
    store: ThreadStore,
) -> None:
    from voidx.agent.adapters.persistence.session_repository import (
        GoalTranscriptCorruption,
    )
    from voidx.agent.application.runtime.dispatcher import RuntimeDispatcher

    spec = GoalSpec(
        objective="ship feature",
        acceptance_condition="tests pass",
        generation="gen-dispatch-corruption",
    )
    state = GoalState.from_spec(
        spec,
        run_id=spec.generation,
        main_session_id="main-dispatch-corruption",
        work_session_id="work-dispatch-corruption",
        evaluator_session_id="eval-dispatch-corruption",
    )
    thread_id = "goal:main-dispatch-corruption:gen-dispatch-corruption"
    await store.ensure_session(
        "main-dispatch-corruption",
        "/workspace",
        profile="goal",
        profile_snapshot=_profile_snapshot(),
    )
    await store.initialize_goal_generation(
        generation=spec.generation,
        main_session_id="main-dispatch-corruption",
        evaluator_session_id="eval-dispatch-corruption",
        work_session_id="work-dispatch-corruption",
        goal_thread_id=thread_id,
        parent_thread_id="main-dispatch-corruption",
        workspace="/workspace",
        profile_id="goal",
        profile_snapshot=_profile_snapshot(),
        thread_profile=GOAL_PROFILE,
        thread_state=AgentThreadState(
            thread_id=thread_id,
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
        protocol=GoalProtocolRecord.submitted(
            protocol_id="protocol-init-dispatch-corruption",
            parent_session_id="main-dispatch-corruption",
            generation=spec.generation,
            phase="init",
            attempt_number=0,
            turn_id="turn-init-dispatch-corruption",
            session_id="main-dispatch-corruption",
            payload=GoalSpecSnapshot.from_spec(
                spec,
                parent_session_id="main-dispatch-corruption",
                profile_snapshot={"profile_id": "goal"},
            ),
        ),
    )

    class CorruptRunner:
        async def run_turn(self, *, thread, profile, input_frame):
            del thread, profile, input_frame
            raise GoalTranscriptCorruption("canonical transcript hash/offset corruption")

    dispatcher = RuntimeDispatcher(
        store=store,
        runner=CorruptRunner(),
        lease_owner="worker-corruption",
    )

    assert await dispatcher.dispatch_once() is None
    failure = await store.get_goal_runtime_failure(spec.generation)
    assert failure is not None
    assert failure.observed_sequence == 0
    assert "transcript" in failure.reason.lower()
    loaded = await store.load(thread_id)
    assert loaded is not None
    assert loaded.state.lifecycle is LifecycleState.FAILED
    assert await store.list_pending_outbox(thread_id) == []
