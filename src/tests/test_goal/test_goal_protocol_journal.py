from __future__ import annotations

import pytest

from voidx.agent.domain.automation.goal import (
    GoalDecision,
    GoalProtocolRecord,
    GoalSpec,
    GoalSpecSnapshot,
    GoalState,
    WorkCheckpoint,
    goal_sequence_number,
    is_goal_terminal,
)
from voidx.agent.domain.thread import LifecycleState, TERMINAL_LIFECYCLES


def test_goal_protocol_sequence_is_strictly_linear() -> None:
    assert goal_sequence_number("init", 0) == 0
    assert goal_sequence_number("checkpoint", 1) == 1
    assert goal_sequence_number("decision", 1) == 2
    assert goal_sequence_number("checkpoint", 2) == 3
    assert goal_sequence_number("decision", 2) == 4

    with pytest.raises(ValueError):
        goal_sequence_number("checkpoint", 0)
    with pytest.raises(ValueError):
        goal_sequence_number("init", 1)
    with pytest.raises(ValueError):
        goal_sequence_number("unknown", 1)


def test_goal_protocol_payloads_are_typed_and_frozen() -> None:
    spec = GoalSpec(
        objective="ship feature",
        acceptance_condition="tests pass",
        generation="gen_1",
    )
    snapshot = GoalSpecSnapshot.from_spec(
        spec,
        parent_session_id="ses_main",
        profile_snapshot={"profile_id": "goal"},
        model_snapshot={"provider": "test", "model": "fake"},
    )
    checkpoint = WorkCheckpoint(
        generation="gen_1",
        attempt_number=1,
        summary="implemented",
        evidence=("src/app.py",),
        changed_files=("src/app.py",),
        verification=("tests pass",),
        next_hint="",
        progress="meaningful",
        work_turn_id="turn_work_1",
    )
    decision = GoalDecision(
        generation="gen_1",
        attempt_number=1,
        status="finished",
        summary="accepted",
        evidence=("tests pass",),
        reason="acceptance verified",
        next_hint="",
        missing_evidence=(),
        progress="meaningful",
    )

    assert snapshot.generation == "gen_1"
    assert checkpoint.evidence == ("src/app.py",)
    assert decision.status == "finished"
    with pytest.raises(Exception):
        checkpoint.summary = "changed"


def test_goal_protocol_record_uses_deterministic_payload_hash() -> None:
    payload = WorkCheckpoint(
        generation="gen_1",
        attempt_number=1,
        summary="implemented",
        work_turn_id="turn_work_1",
    )
    first = GoalProtocolRecord.submitted(
        protocol_id="protocol_1",
        parent_session_id="ses_main",
        generation="gen_1",
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn_work_1",
        session_id="ses_work",
        payload=payload,
    )
    second = GoalProtocolRecord.submitted(
        protocol_id="protocol_2",
        parent_session_id="ses_main",
        generation="gen_1",
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn_work_2",
        session_id="ses_work",
        payload=payload,
    )

    assert first.sequence_number == 1
    assert first.payload_type == "WorkCheckpoint"
    assert first.payload_hash == second.payload_hash
    assert first.status == "submitted"
    assert first.projected_at is None


def test_goal_terminal_lifecycles_include_blocked_but_not_needs_user() -> None:
    assert is_goal_terminal(LifecycleState.COMPLETED)
    assert is_goal_terminal(LifecycleState.BLOCKED)
    assert is_goal_terminal(LifecycleState.FAILED)
    assert is_goal_terminal(LifecycleState.CANCELLED)
    assert not is_goal_terminal(LifecycleState.NEEDS_USER)
    assert not is_goal_terminal(LifecycleState.RUNNING)


@pytest.fixture
def isolated_store(tmp_path):
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore

    return ThreadStore(tmp_path / "store.db")


def _init_record() -> GoalProtocolRecord:
    spec = GoalSpec(
        objective="ship feature",
        acceptance_condition="tests pass",
        generation="gen_store",
    )
    payload = GoalSpecSnapshot.from_spec(spec, parent_session_id="ses_main")
    return GoalProtocolRecord.submitted(
        protocol_id="protocol_init",
        parent_session_id="ses_main",
        generation="gen_store",
        phase="init",
        attempt_number=0,
        turn_id="turn_init",
        session_id="ses_main",
        payload=payload,
    )


def _checkpoint_record(*, summary: str = "implemented", protocol_id: str = "protocol_checkpoint") -> GoalProtocolRecord:
    payload = WorkCheckpoint(
        generation="gen_store",
        attempt_number=1,
        summary=summary,
        work_turn_id="turn_work",
    )
    return GoalProtocolRecord.submitted(
        protocol_id=protocol_id,
        parent_session_id="ses_main",
        generation="gen_store",
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn_work",
        session_id="ses_work",
        payload=payload,
    )


@pytest.mark.asyncio
async def test_goal_journal_submission_is_idempotent_by_position_and_payload(isolated_store) -> None:
    record = _init_record()

    first = await isolated_store.submit_goal_protocol(record)
    retry = await isolated_store.submit_goal_protocol(
        record.model_copy(update={"protocol_id": "protocol_init_retry", "turn_id": "turn_init_retry"})
    )

    assert first == retry
    assert (await isolated_store.list_goal_protocols("gen_store")) == [record]


@pytest.mark.asyncio
async def test_goal_journal_rejects_conflicting_payload_at_same_position(isolated_store) -> None:
    await isolated_store.submit_goal_protocol(_init_record())
    await isolated_store.project_goal_protocol("protocol_init")
    await isolated_store.submit_goal_protocol(_checkpoint_record())

    with pytest.raises(Exception, match="payload"):
        await isolated_store.submit_goal_protocol(
            _checkpoint_record(summary="different", protocol_id="protocol_checkpoint_conflict")
        )


@pytest.mark.asyncio
async def test_goal_journal_rejects_phase_when_previous_position_is_not_projected(isolated_store) -> None:
    await isolated_store.submit_goal_protocol(_init_record())

    with pytest.raises(Exception, match="preceding|projected"):
        await isolated_store.submit_goal_protocol(_checkpoint_record())


@pytest.mark.asyncio
async def test_goal_journal_projection_is_idempotent_and_monotonic(isolated_store) -> None:
    record = _init_record()
    submitted = await isolated_store.submit_goal_protocol(record)
    projected = await isolated_store.project_goal_protocol(submitted.protocol_id)
    retry = await isolated_store.project_goal_protocol(submitted.protocol_id)

    assert projected.status == "projected"
    assert projected.projected_at is not None
    assert retry == projected
    assert (await isolated_store.get_goal_protocol(submitted.protocol_id)) == projected


@pytest.mark.asyncio
async def test_goal_journal_rejects_projection_of_unknown_record(isolated_store) -> None:
    with pytest.raises(KeyError):
        await isolated_store.project_goal_protocol("missing")


def test_goal_lifecycle_has_specialized_terminal_semantics() -> None:
    assert "active" not in GoalState.model_fields
    assert LifecycleState.BLOCKED not in TERMINAL_LIFECYCLES
    assert is_goal_terminal(LifecycleState.BLOCKED)
