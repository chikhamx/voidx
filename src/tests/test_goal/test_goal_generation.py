from __future__ import annotations

import pytest

from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.domain.agent_profile import AgentProfileSnapshot
from voidx.agent.domain.automation.goal import (
    GOAL_PROFILE,
    GoalDecision,
    GoalProtocolRecord,
    GoalSpec,
    GoalSpecSnapshot,
    GoalState,
    WorkCheckpoint,
)
from voidx.agent.domain.thread import AgentThreadState, LifecycleState
from tests.goal_protocol_helpers import submit_fenced_goal_protocol


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


def _boundary_kwargs(store: ThreadStore) -> dict:
    spec = GoalSpec(
        objective="ship feature",
        acceptance_condition="tests pass",
        generation="gen_1",
    )
    snapshot = GoalSpecSnapshot.from_spec(
        spec,
        parent_session_id="main_1",
        profile_snapshot={"profile_id": "goal"},
    )
    state = GoalState.from_spec(
        spec,
        run_id="gen_1",
        main_session_id="main_1",
        work_session_id="work_1",
        evaluator_session_id="eval_1",
    )
    record = GoalProtocolRecord.submitted(
        protocol_id="protocol_init_1",
        parent_session_id="main_1",
        generation="gen_1",
        phase="init",
        attempt_number=0,
        turn_id="turn_init_1",
        session_id="main_1",
        payload=snapshot,
    )
    return {
        "generation": "gen_1",
        "main_session_id": "main_1",
        "evaluator_session_id": "eval_1",
        "work_session_id": "work_1",
        "goal_thread_id": "goal:main_1:gen_1",
        "parent_thread_id": "main_thread",
        "workspace": "/workspace",
        "profile_id": "goal",
        "profile_snapshot": _profile_snapshot(),
        "thread_profile": GOAL_PROFILE,
        "thread_state": AgentThreadState(
            thread_id="goal:main_1:gen_1",
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
        "protocol": record,
    }


@pytest.mark.asyncio
async def test_boundary_i_binds_three_sessions_and_projects_init(store: ThreadStore) -> None:
    await store.ensure_session(
        "main_1", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )

    binding = await store.initialize_goal_generation(**_boundary_kwargs(store))

    assert binding.generation == "gen_1"
    assert binding.main_session_id == "main_1"
    assert binding.work_session_id == "work_1"
    assert binding.evaluator_session_id == "eval_1"
    assert await store.get_session("work_1") is not None
    assert await store.get_session("eval_1") is not None

    protocols = await store.list_goal_protocols("gen_1")
    assert [(item.sequence_number, item.status) for item in protocols] == [(0, "projected")]

    loaded = await store.load("goal:main_1:gen_1")
    assert loaded is not None
    projected_state = GoalState.model_validate(loaded.state.context["goal_run"])
    assert projected_state.projected_sequence_number == 0
    assert projected_state.current_phase == "work"

    pending = await store.list_pending_outbox("goal:main_1:gen_1")
    assert len(pending) == 1
    assert pending[0].kind == "goal_prompt"
    assert pending[0].payload["phase"] == "work"
    assert pending[0].payload["generation"] == "gen_1"


@pytest.mark.asyncio
async def test_boundary_i_is_idempotent_and_rejects_binding_conflict(store: ThreadStore) -> None:
    await store.ensure_session(
        "main_1", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    kwargs = _boundary_kwargs(store)

    first = await store.initialize_goal_generation(**kwargs)
    second = await store.initialize_goal_generation(**kwargs)
    assert second == first
    assert len(await store.list_pending_outbox("goal:main_1:gen_1")) == 1

    with pytest.raises(Exception, match="binding"):
        await store.initialize_goal_generation(
            **{**kwargs, "work_session_id": "work_other"}
        )


@pytest.mark.asyncio
async def test_boundary_a_projects_checkpoint_and_enqueues_evaluator(store: ThreadStore) -> None:
    await store.ensure_session(
        "main_1", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs(store))

    checkpoint = WorkCheckpoint(
        generation="gen_1",
        attempt_number=1,
        summary="implemented the feature",
        evidence=("src/app.py",),
        verification=("tests pass",),
        progress="meaningful",
        work_turn_id="turn_work_1",
    )
    record = GoalProtocolRecord.submitted(
        protocol_id="protocol_checkpoint_1",
        parent_session_id="main_1",
        generation="gen_1",
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn_work_1",
        session_id="work_1",
        payload=checkpoint,
    )
    await submit_fenced_goal_protocol(store, record)

    projected = await store.project_goal_protocol(record.protocol_id)

    assert projected.status == "projected"
    loaded = await store.load("goal:main_1:gen_1")
    assert loaded is not None
    state = GoalState.model_validate(loaded.state.context["goal_run"])
    assert state.projected_sequence_number == 1
    assert state.current_phase == "evaluator"
    assert state.phase_status == "running"
    assert state.last_work_checkpoint == checkpoint
    assert state.last_protocol_id == record.protocol_id
    pending = await store.list_pending_outbox("goal:main_1:gen_1")
    assert len(pending) == 1
    assert pending[0].kind == "goal_prompt"
    assert pending[0].payload["phase"] == "evaluator"
    assert pending[0].payload["attempt_number"] == 1
    assert pending[0].payload["checkpoint"]["summary"] == checkpoint.summary


@pytest.mark.asyncio
async def test_boundary_b_continue_projects_and_enqueues_next_work(store: ThreadStore) -> None:
    await store.ensure_session(
        "main_1", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs(store))

    checkpoint = WorkCheckpoint(
        generation="gen_1",
        attempt_number=1,
        summary="partial implementation",
        progress="partial",
        work_turn_id="turn_work_1",
    )
    checkpoint_record = GoalProtocolRecord.submitted(
        protocol_id="protocol_checkpoint_1",
        parent_session_id="main_1",
        generation="gen_1",
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn_work_1",
        session_id="work_1",
        payload=checkpoint,
    )
    await submit_fenced_goal_protocol(store, checkpoint_record)
    await store.project_goal_protocol(checkpoint_record.protocol_id)
    evaluator_outbox = (await store.list_pending_outbox("goal:main_1:gen_1"))[0]
    assert evaluator_outbox.payload["phase"] == "evaluator"

    decision = GoalDecision(
        generation="gen_1",
        attempt_number=1,
        status="continue",
        summary="more evidence is needed",
        reason="missing integration evidence",
        next_hint="verify the integration path",
        progress="partial",
    )
    decision_record = GoalProtocolRecord.submitted(
        protocol_id="protocol_decision_1",
        parent_session_id="main_1",
        generation="gen_1",
        phase="decision",
        attempt_number=1,
        turn_id="turn_eval_1",
        session_id="eval_1",
        payload=decision,
    )
    await submit_fenced_goal_protocol(store, decision_record)

    projected = await store.project_goal_protocol(decision_record.protocol_id)

    assert projected.status == "projected"
    loaded = await store.load("goal:main_1:gen_1")
    assert loaded is not None
    state = GoalState.model_validate(loaded.state.context["goal_run"])
    assert state.projected_sequence_number == 2
    assert state.current_phase == "work"
    assert state.phase_status == "running"
    assert state.attempt_count == 1
    assert state.last_evaluator_summary == decision.summary
    assert state.last_protocol_id == decision_record.protocol_id
    pending = await store.list_pending_outbox("goal:main_1:gen_1")
    assert len(pending) == 1
    assert pending[0].kind == "goal_prompt"
    assert pending[0].payload["phase"] == "work"
    assert pending[0].payload["attempt_number"] == 2


@pytest.mark.asyncio
async def test_boundary_b_finished_projects_goal_terminal_without_successor(store: ThreadStore) -> None:
    await store.ensure_session(
        "main_1", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs(store))

    checkpoint = WorkCheckpoint(
        generation="gen_1",
        attempt_number=1,
        summary="implementation complete",
        progress="meaningful",
        work_turn_id="turn_work_1",
    )
    checkpoint_record = GoalProtocolRecord.submitted(
        protocol_id="protocol_checkpoint_1",
        parent_session_id="main_1",
        generation="gen_1",
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn_work_1",
        session_id="work_1",
        payload=checkpoint,
    )
    await submit_fenced_goal_protocol(store, checkpoint_record)
    await store.project_goal_protocol(checkpoint_record.protocol_id)
    evaluator_outbox = (await store.list_pending_outbox("goal:main_1:gen_1"))[0]
    assert evaluator_outbox.payload["phase"] == "evaluator"

    decision = GoalDecision(
        generation="gen_1",
        attempt_number=1,
        status="finished",
        summary="accepted",
        evidence=("tests pass",),
        reason="acceptance condition verified",
        progress="meaningful",
    )
    decision_record = GoalProtocolRecord.submitted(
        protocol_id="protocol_decision_1",
        parent_session_id="main_1",
        generation="gen_1",
        phase="decision",
        attempt_number=1,
        turn_id="turn_eval_1",
        session_id="eval_1",
        payload=decision,
    )
    await submit_fenced_goal_protocol(store, decision_record)

    await store.project_goal_protocol(decision_record.protocol_id)

    loaded = await store.load("goal:main_1:gen_1")
    assert loaded is not None
    state = GoalState.model_validate(loaded.state.context["goal_run"])
    assert state.projected_sequence_number == 2
    assert state.attempt_count == 1
    assert state.phase_status == "running"
    assert state.last_evaluator_summary == decision.summary
    assert state.blocked_reason == ""
    assert loaded.state.lifecycle == LifecycleState.COMPLETED
    assert await store.list_pending_outbox("goal:main_1:gen_1") == []
