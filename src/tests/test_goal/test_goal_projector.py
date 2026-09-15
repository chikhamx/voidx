from __future__ import annotations

import pytest

from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.application.automation.goal.projector import GoalProjector
from voidx.agent.application.automation.goal.recovery import GoalRecovery
from voidx.agent.domain.agent_profile import AgentProfileSnapshot
from voidx.agent.domain.automation.goal import (
    GOAL_PROFILE,
    GoalDecision,
    GoalProtocolRecord,
    GoalSpec,
    GoalSpecSnapshot,
    GoalState,
    WorkCheckpoint,
    GoalRuntimeFailure,
)
from voidx.agent.domain.thread import AgentThreadState, LifecycleState
from voidx.agent.ports.persistence import GoalProtocolConflict
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


def _boundary_kwargs() -> dict:
    spec = GoalSpec(
        objective="ship feature",
        acceptance_condition="tests pass",
        generation="gen-projector",
    )
    snapshot = GoalSpecSnapshot.from_spec(
        spec,
        parent_session_id="main-projector",
        profile_snapshot={"profile_id": "goal"},
    )
    state = GoalState.from_spec(
        spec,
        run_id=spec.generation,
        main_session_id="main-projector",
        work_session_id="work-projector",
        evaluator_session_id="eval-projector",
    )
    return {
        "generation": spec.generation,
        "main_session_id": "main-projector",
        "evaluator_session_id": "eval-projector",
        "work_session_id": "work-projector",
        "goal_thread_id": "goal:parent-projector:gen-projector",
        "parent_thread_id": "parent-projector",
        "workspace": "/workspace",
        "profile_id": "goal",
        "profile_snapshot": _profile_snapshot(),
        "thread_profile": GOAL_PROFILE,
        "thread_state": AgentThreadState(
            thread_id="goal:parent-projector:gen-projector",
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
        "protocol": GoalProtocolRecord.submitted(
            protocol_id="protocol-init-projector",
            parent_session_id="main-projector",
            generation=spec.generation,
            phase="init",
            attempt_number=0,
            turn_id="turn-init-projector",
            session_id="main-projector",
            payload=snapshot,
        ),
    }


async def _initialize(store: ThreadStore) -> str:
    await store.ensure_session(
        "main-projector",
        "/workspace",
        profile="goal",
        profile_snapshot=_profile_snapshot(),
    )
    kwargs = _boundary_kwargs()
    await store.initialize_goal_generation(**kwargs)
    return kwargs["generation"]


@pytest.mark.asyncio
async def test_projector_replays_submitted_record_idempotently(store: ThreadStore) -> None:
    generation = await _initialize(store)
    checkpoint = WorkCheckpoint(
        generation=generation,
        attempt_number=1,
        summary="implementation captured",
        work_turn_id="turn-work-projector",
    )
    record = GoalProtocolRecord.submitted(
        protocol_id="protocol-checkpoint-projector",
        parent_session_id="main-projector",
        generation=generation,
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn-work-projector",
        session_id="work-projector",
        payload=checkpoint,
    )
    await submit_fenced_goal_protocol(store, record)

    projector = GoalProjector(store=store)
    first = await projector.project(record.protocol_id)
    second = await projector.project(record.protocol_id)

    assert first.status == "projected"
    assert second == first
    state = await store.load("goal:parent-projector:gen-projector")
    assert state is not None
    assert GoalState.model_validate(state.state.context["goal_run"]).projected_sequence_number == 1
    pending = await store.list_pending_outbox("goal:parent-projector:gen-projector")
    assert len(pending) == 1
    assert pending[0].payload["sequence_number"] == 2


@pytest.mark.asyncio
async def test_recovery_projects_journal_without_starting_a_runner(store: ThreadStore) -> None:
    generation = await _initialize(store)
    checkpoint = WorkCheckpoint(
        generation=generation,
        attempt_number=1,
        summary="implementation captured",
        work_turn_id="turn-work-projector",
    )
    record = GoalProtocolRecord.submitted(
        protocol_id="protocol-checkpoint-recovery",
        parent_session_id="main-projector",
        generation=generation,
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn-work-projector",
        session_id="work-projector",
        payload=checkpoint,
    )
    await submit_fenced_goal_protocol(store, record)

    recovery = GoalRecovery(store=store)
    await recovery.recover_generation(generation)
    await recovery.recover_generation(generation)

    protocols = await store.list_goal_protocols(generation)
    assert [(item.sequence_number, item.status) for item in protocols] == [
        (0, "projected"),
        (1, "projected"),
    ]
    pending = await store.list_pending_outbox("goal:parent-projector:gen-projector")
    assert len(pending) == 1
    assert pending[0].payload["phase"] == "evaluator"
    assert pending[0].payload["sequence_number"] == 2


@pytest.mark.asyncio
async def test_recovery_durably_fails_a_sequence_hole(store: ThreadStore) -> None:
    generation = await _initialize(store)
    stored = await store.list_goal_protocols(generation)
    decision = GoalDecision(
        generation=generation,
        attempt_number=1,
        status="continue",
        summary="more evidence is needed",
    )
    hole = GoalProtocolRecord.submitted(
        protocol_id="protocol-decision-hole",
        parent_session_id="main-projector",
        generation=generation,
        phase="decision",
        attempt_number=1,
        turn_id="turn-decision-hole",
        session_id="eval-projector",
        payload=decision,
    )

    async def list_with_hole(_generation: str):
        return [*stored, hole]

    store.list_goal_protocols = list_with_hole
    await GoalRecovery(store=store).recover_generation(generation)

    failure = await store.get_goal_runtime_failure(generation)
    assert failure is not None
    assert "sequence hole" in failure.reason


@pytest.mark.asyncio
async def test_runtime_failure_atomically_stops_projection_and_publishes_summary(
    store: ThreadStore,
) -> None:
    generation = await _initialize(store)
    record = GoalProtocolRecord.submitted(
        protocol_id="protocol-checkpoint-before-failure",
        parent_session_id="main-projector",
        generation=generation,
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn-work-before-failure",
        session_id="work-projector",
        payload=WorkCheckpoint(
            generation=generation,
            attempt_number=1,
            summary="checkpoint persisted before corruption was detected",
            work_turn_id="turn-work-before-failure",
        ),
    )
    _, attempt = await submit_fenced_goal_protocol(store, record)
    failure = GoalRuntimeFailure(
        generation=generation,
        observed_sequence=0,
        reason="Goal journal sequence invariant failed",
        evidence=("expected sequence 1", "observed conflicting payload"),
    )

    stored = await store.fail_goal_generation(failure)
    repeated = await store.fail_goal_generation(failure)

    assert stored == failure
    assert repeated == failure
    assert await store.get_goal_runtime_failure(generation) == failure
    loaded = await store.load("goal:parent-projector:gen-projector")
    assert loaded is not None
    assert loaded.state.lifecycle == LifecycleState.FAILED
    assert loaded.state.lifecycle_decision is not None
    assert loaded.state.lifecycle_decision.outcome == "failed"
    assert loaded.state.lifecycle_decision.reason == failure.reason
    recovered_attempt = await store.get_attempt(attempt.attempt_id)
    assert recovered_attempt is not None
    assert recovered_attempt.status == "committed"
    assert await store.list_pending_outbox(loaded.thread.thread_id) == []
    binding = await store.get_goal_generation(generation)
    assert binding is not None
    assert binding.terminal_at is not None

    summaries = await store.list_goal_public_summaries("main-projector")
    assert len(summaries) == 1
    assert summaries[0]["generation"] == generation
    assert summaries[0]["kind"] == "runtime_failure"
    assert failure.reason in summaries[0]["summary"]

    before_conflict = await store.load("goal:parent-projector:gen-projector")
    conflicting = failure.model_copy(update={"reason": "different failure"})
    with pytest.raises(GoalProtocolConflict, match="failure conflict"):
        await store.fail_goal_generation(conflicting)
    after_conflict = await store.load("goal:parent-projector:gen-projector")
    assert before_conflict is not None and after_conflict is not None
    assert after_conflict.state_version == before_conflict.state_version
    assert await store.get_goal_runtime_failure(generation) == failure
    assert await store.get_attempt(attempt.attempt_id) == recovered_attempt
    assert await store.list_goal_public_summaries("main-projector") == summaries

    with pytest.raises(GoalProtocolConflict, match="terminal"):
        await store.project_goal_protocol(record.protocol_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_lifecycle", "expected_kind"),
    [
        ("finished", LifecycleState.COMPLETED, "completed"),
        ("blocked", LifecycleState.BLOCKED, "blocked"),
    ],
)
async def test_terminal_decision_atomically_enqueues_structured_public_summary(
    store: ThreadStore,
    status: str,
    expected_lifecycle: LifecycleState,
    expected_kind: str,
) -> None:
    generation = await _initialize(store)
    checkpoint = GoalProtocolRecord.submitted(
        protocol_id=f"protocol-checkpoint-summary-{status}",
        parent_session_id="main-projector",
        generation=generation,
        phase="checkpoint",
        attempt_number=1,
        turn_id=f"turn-work-summary-{status}",
        session_id="work-projector",
        payload=WorkCheckpoint(
            generation=generation,
            attempt_number=1,
            summary="implementation captured",
            work_turn_id=f"turn-work-summary-{status}",
        ),
    )
    await submit_fenced_goal_protocol(store, checkpoint)
    await store.project_goal_protocol(checkpoint.protocol_id)
    decision = GoalProtocolRecord.submitted(
        protocol_id=f"protocol-decision-summary-{status}",
        parent_session_id="main-projector",
        generation=generation,
        phase="decision",
        attempt_number=1,
        turn_id=f"turn-eval-summary-{status}",
        session_id="eval-projector",
        payload=GoalDecision(
            generation=generation,
            attempt_number=1,
            status=status,
            summary="acceptance evidence checked",
            reason="verified" if status == "finished" else "external dependency",
        ),
    )
    await submit_fenced_goal_protocol(store, decision)

    await store.project_goal_protocol(decision.protocol_id)

    loaded = await store.load("goal:parent-projector:gen-projector")
    assert loaded is not None
    assert loaded.state.lifecycle is expected_lifecycle
    summaries = await store.list_goal_public_summaries("main-projector")
    assert len(summaries) == 1
    assert summaries[0]["kind"] == expected_kind
    payload = summaries[0]["payload"]
    assert payload == {
        "generation": generation,
        "phase": "evaluator",
        "outcome": expected_kind,
        "objective_summary": "ship feature",
        "attempt_count": 1,
        "summary": "acceptance evidence checked",
        "created_at": summaries[0]["created_at"],
    }
    assert summaries[0]["delivered_at"] is None


@pytest.mark.asyncio
async def test_public_summary_delivery_is_idempotent_after_file_before_ack_crash(
    store: ThreadStore,
    monkeypatch,
) -> None:
    from voidx.persistence.jsonl import read_session_records

    generation = await _initialize(store)
    await store.fail_goal_generation(
        GoalRuntimeFailure(
            generation=generation,
            observed_sequence=0,
            reason="journal corruption",
        )
    )
    summary = (await store.list_goal_public_summaries("main-projector"))[0]
    real_ack = store._ack_goal_public_summary_delivery
    calls = 0

    async def crash_once(summary_id: str, message_id: int) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("crash after JSONL append")
        return await real_ack(summary_id, message_id)

    monkeypatch.setattr(store, "_ack_goal_public_summary_delivery", crash_once)
    with pytest.raises(RuntimeError, match="crash after JSONL append"):
        await store.deliver_goal_public_summary(summary["summary_id"])

    await store.deliver_goal_public_summary(summary["summary_id"])
    records = await read_session_records("main-projector", "messages.jsonl")
    matching = [
        record
        for record in records or []
        if (record.get("additional_kwargs") or {}).get("goal_public_summary_id")
        == summary["summary_id"]
    ]
    assert len(matching) == 1
    delivered = (await store.list_goal_public_summaries("main-projector"))[0]
    assert delivered["delivered_at"] is not None
    assert (await store.get_session("main-projector")).message_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["project", "wakeup", "corruption"])
@pytest.mark.parametrize("loss", ["expired", "new-owner"])
async def test_recovery_write_boundary_fences_lease(store, monkeypatch, boundary, loss):
    generation = await _initialize(store)
    if boundary == "project":
        record = GoalProtocolRecord.submitted(
            protocol_id="checkpoint-atomic", parent_session_id="main-projector",
            generation=generation, phase="checkpoint", attempt_number=1,
            turn_id="work-atomic", session_id="work-projector",
            payload=WorkCheckpoint(generation=generation, attempt_number=1,
                                   summary="done", work_turn_id="work-atomic"),
        )
        await submit_fenced_goal_protocol(store, record)
    elif boundary == "wakeup":
        await store._write(lambda conn: conn.execute("DELETE FROM runtime_outbox"))
    else:
        await store._write(lambda conn: conn.execute("DELETE FROM goal_protocol_records"))

    async def snapshot():
        tables = ["goal_generations", "agent_threads", "agent_thread_state",
                  "goal_protocol_records", "runtime_outbox", "goal_runtime_failures",
                  "goal_public_summary_outbox"]
        return {table: [dict(row) for row in await store._all(f"SELECT * FROM {table}")]
                for table in tables}

    baseline = await snapshot()
    method = {"project": "project_goal_protocol", "wakeup": "ensure_goal_phase_outbox",
              "corruption": "fail_goal_generation"}[boundary]
    original = getattr(store, method)
    foreign = None

    async def preempt(*args, **kwargs):
        nonlocal foreign
        lease = await store.get_goal_generation_lease(generation)
        assert await store.renew_goal_generation_lease(generation, lease["lease_owner"], lease_seconds=30)
        await store._write(lambda conn: conn.execute(
            "UPDATE goal_recovery_leases SET lease_expires_at = 0 WHERE generation = ?", (generation,)))
        if loss == "new-owner":
            assert await store.acquire_goal_generation_lease(generation, "foreign", lease_seconds=60)
        foreign = await store.get_goal_generation_lease(generation)
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, method, preempt)
    with pytest.raises(GoalProtocolConflict, match="lease"):
        await GoalRecovery(store=store).recover_generation(generation)
    assert await snapshot() == baseline
    if loss == "new-owner":
        assert await store.get_goal_generation_lease(generation) == foreign


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["init", "project-init"])
@pytest.mark.parametrize("loss", ["missing", "expired", "new-owner"])
async def test_init_write_requires_live_recovery_owner(store, boundary, loss):
    kwargs = _boundary_kwargs()
    generation = kwargs["generation"]
    await store.ensure_session("main-projector", "/workspace", profile="goal",
                               profile_snapshot=_profile_snapshot())
    await store.submit_goal_protocol(kwargs["protocol"])
    if loss != "missing":
        assert await store.acquire_goal_generation_lease(generation, "old", lease_seconds=30)
        await store._write(lambda conn: conn.execute(
            "UPDATE goal_recovery_leases SET lease_expires_at = 0 WHERE generation = ?", (generation,)))
        if loss == "new-owner":
            assert await store.acquire_goal_generation_lease(generation, "foreign", lease_seconds=60)
    before = await store.get_goal_protocol(kwargs["protocol"].protocol_id)
    with pytest.raises(GoalProtocolConflict, match="lease"):
        if boundary == "init":
            await store.initialize_goal_generation(**kwargs, lease_owner="old")
        else:
            await GoalProjector(store=store).project(before.protocol_id, lease_owner="old")
    assert await store.get_goal_generation(generation) is None
    assert await store.get_goal_protocol(before.protocol_id) == before
    assert await store._all("SELECT * FROM runtime_outbox") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["init", "project-init", "wakeup", "corruption", "summaries"])
async def test_live_recovery_owner_holds_sqlite_write_lock_through_commit(store, tmp_path, boundary):
    import sqlite3

    kwargs = _boundary_kwargs()
    generation = kwargs["generation"]
    if boundary in ("wakeup", "corruption", "summaries"):
        await _initialize(store)
    else:
        await store.ensure_session("main-projector", "/workspace", profile="goal",
                                   profile_snapshot=_profile_snapshot())
        await store.submit_goal_protocol(kwargs["protocol"])
    if boundary == "summaries":
        await store.fail_goal_generation(
            GoalRuntimeFailure(generation=generation, observed_sequence=0, reason="corrupt")
        )
    assert await store.acquire_goal_generation_lease(generation, "owner", lease_seconds=60)
    statements, blocked, unexpected = [], [], []

    def trace(sql):
        statements.append(sql)
        if sql.startswith("SELECT lease_owner, lease_expires_at FROM goal_recovery_leases"):
            try:
                contender.execute("UPDATE goal_recovery_leases SET lease_owner = 'foreign'")
                contender.commit()
            except sqlite3.OperationalError as exc:
                blocked.append(str(exc))
                contender.rollback()
            else:
                unexpected.append("foreign write succeeded during lease check")

    # The callback runs on the persistence worker thread.
    contender = sqlite3.connect(tmp_path / "store.db", timeout=0, check_same_thread=False)
    store._conn.set_trace_callback(trace)
    try:
        if boundary == "init":
            await store.initialize_goal_generation(**kwargs, lease_owner="owner")
        elif boundary == "project-init":
            await GoalProjector(store=store).project(kwargs["protocol"].protocol_id, lease_owner="owner")
        elif boundary == "summaries":
            assert await store.deliver_goal_public_summaries(generation=generation, lease_owner="owner") == 1
        elif boundary == "wakeup":
            await store.ensure_goal_phase_outbox(generation, lease_owner="owner")
        else:
            await store.fail_goal_generation(
                GoalRuntimeFailure(generation=generation, observed_sequence=0, reason="corrupt"),
                lease_owner="owner",
            )
    finally:
        store._conn.set_trace_callback(None)
        contender.close()
    assert not unexpected
    assert blocked == ["database is locked"]
    if boundary == "summaries":
        fence_index = next(i for i, sql in enumerate(statements)
                           if sql.startswith("SELECT lease_owner, lease_expires_at FROM goal_recovery_leases"))
        assert statements[fence_index - 1] == "BEGIN IMMEDIATE"
    else:
        assert statements[0] == "BEGIN IMMEDIATE"
    assert statements[-1] == "COMMIT"
    lease = await store.get_goal_generation_lease(generation)
    assert lease["lease_owner"] == "owner"
    if boundary == "corruption":
        assert (await store.get_goal_runtime_failure(generation)).reason == "corrupt"
        assert await store.release_goal_generation_lease(generation, "owner")
        assert await store.get_goal_generation_lease(generation) is None
