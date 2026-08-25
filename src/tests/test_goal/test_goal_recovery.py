from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.application.automation.goal.goal_service import GoalService
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
from voidx.agent.ports.persistence import GoalProtocolConflict
from tests.goal_protocol_helpers import submit_fenced_goal_protocol


@dataclass
class FakeGoalScheduler:
    calls: list[tuple[str, GoalSpec]] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    pump_starts: int = 0

    async def run_goal(self, parent_thread_id: str, spec: GoalSpec):
        self.calls.append((parent_thread_id, spec))

    def register_goal_thread(self, thread_id: str) -> None:
        self.registered.append(thread_id)

    def unregister_goal_thread(self, thread_id: str) -> None:
        del thread_id

    def start_pump(self) -> None:
        self.pump_starts += 1


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
        generation="gen-recovery",
    )
    snapshot = GoalSpecSnapshot.from_spec(
        spec,
        parent_session_id="main-recovery",
        profile_snapshot={"profile_id": "goal"},
    )
    state = GoalState.from_spec(
        spec,
        run_id=spec.generation,
        main_session_id="main-recovery",
        work_session_id="work-recovery",
        evaluator_session_id="eval-recovery",
    )
    return {
        "generation": spec.generation,
        "main_session_id": "main-recovery",
        "evaluator_session_id": "eval-recovery",
        "work_session_id": "work-recovery",
        "goal_thread_id": "goal:parent-recovery:gen-recovery",
        "parent_thread_id": "parent-recovery",
        "workspace": "/workspace",
        "profile_id": "goal",
        "profile_snapshot": _profile_snapshot(),
        "thread_profile": GOAL_PROFILE,
        "thread_state": AgentThreadState(
            thread_id="goal:parent-recovery:gen-recovery",
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
        "protocol": GoalProtocolRecord.submitted(
            protocol_id="protocol-init-recovery",
            parent_session_id="main-recovery",
            generation=spec.generation,
            phase="init",
            attempt_number=0,
            turn_id="turn-init-recovery",
            session_id="main-recovery",
            payload=snapshot,
        ),
    }


async def _initialize(store: ThreadStore) -> str:
    await store.ensure_session(
        "main-recovery",
        "/workspace",
        profile="goal",
        profile_snapshot=_profile_snapshot(),
    )
    kwargs = _boundary_kwargs()
    await store.initialize_goal_generation(**kwargs)
    return kwargs["generation"]


def _service(store: ThreadStore, scheduler: FakeGoalScheduler) -> GoalService:
    return GoalService(store=store, scheduler=scheduler, workspace="/workspace")


@pytest.mark.asyncio
async def test_resume_generation_replays_submitted_protocol_without_running_model(
    store: ThreadStore,
) -> None:
    generation = await _initialize(store)
    checkpoint = WorkCheckpoint(
        generation=generation,
        attempt_number=1,
        summary="implementation captured",
        work_turn_id="turn-work-recovery",
    )
    record = GoalProtocolRecord.submitted(
        protocol_id="protocol-checkpoint-recovery",
        parent_session_id="main-recovery",
        generation=generation,
        phase="checkpoint",
        attempt_number=1,
        turn_id="turn-work-recovery",
        session_id="work-recovery",
        payload=checkpoint,
    )
    source_outbox, attempt = await submit_fenced_goal_protocol(store, record)
    scheduler = FakeGoalScheduler()

    status = await _service(store, scheduler).resume_generation(generation)

    assert status is not None
    assert scheduler.calls == []
    assert scheduler.registered == ["goal:parent-recovery:gen-recovery"]
    assert scheduler.pump_starts == 1
    recovered_attempt = await store.get_attempt(attempt.attempt_id)
    assert recovered_attempt is not None
    assert recovered_attempt.status == "committed"
    protocols = await store.list_goal_protocols(generation)
    assert [(item.sequence_number, item.status) for item in protocols] == [
        (0, "projected"),
        (1, "projected"),
    ]
    pending = await store.list_pending_outbox("goal:parent-recovery:gen-recovery")
    assert len(pending) == 1
    assert pending[0].payload["phase"] == "evaluator"
    assert pending[0].payload["sequence_number"] == 2


@pytest.mark.asyncio
async def test_resume_generation_recreates_missing_phase_outbox_idempotently(
    store: ThreadStore,
) -> None:
    generation = await _initialize(store)
    initial = (await store.list_pending_outbox("goal:parent-recovery:gen-recovery"))[0]
    await store.ack_outbox(initial.outbox_id)
    scheduler = FakeGoalScheduler()
    service = _service(store, scheduler)

    first = await service.resume_generation(generation)
    second = await service.resume_generation(generation)

    assert first is not None
    assert second is not None
    assert scheduler.calls == []
    pending = await store.list_pending_outbox("goal:parent-recovery:gen-recovery")
    assert len(pending) == 1
    assert pending[0].payload["phase"] == "work"
    assert pending[0].payload["sequence_number"] == 1


@pytest.mark.asyncio
async def test_resume_generation_durably_fails_protocol_sequence_hole(
    store: ThreadStore,
) -> None:
    generation = await _initialize(store)
    stored = await store.list_goal_protocols(generation)
    decision = GoalDecision(
        generation=generation,
        attempt_number=1,
        status="continue",
        summary="needs more evidence",
    )
    hole = GoalProtocolRecord.submitted(
        protocol_id="protocol-decision-hole",
        parent_session_id="main-recovery",
        generation=generation,
        phase="decision",
        attempt_number=1,
        turn_id="turn-decision-hole",
        session_id="eval-recovery",
        payload=decision,
    )

    async def list_with_hole(_generation: str):
        return [*stored, hole]

    store.list_goal_protocols = list_with_hole
    status = await _service(store, FakeGoalScheduler()).resume_generation(generation)

    assert status is not None
    assert status.state == "failed"
    failure = await store.get_goal_runtime_failure(generation)
    assert failure is not None
    assert failure.observed_sequence == 0
    assert "sequence" in failure.reason.lower()
    loaded = await store.load("goal:parent-recovery:gen-recovery")
    assert loaded is not None
    assert loaded.state.lifecycle is LifecycleState.FAILED
    assert await store.list_pending_outbox(loaded.thread.thread_id) == []


@pytest.mark.asyncio
async def test_resume_generation_does_not_revive_outbox_with_existing_attempt(
    store: ThreadStore,
) -> None:
    generation = await _initialize(store)
    thread_id = "goal:parent-recovery:gen-recovery"
    initial = (await store.list_pending_outbox(thread_id))[0]
    await store.begin_attempt(
        thread_id=thread_id,
        source_outbox_id=initial.outbox_id,
        input_frame={"kind": initial.kind, **initial.payload},
        expected_state_version=initial.expected_state_version,
        lease_owner="recovery-test",
        lease_seconds=60,
    )
    await store.ack_outbox(initial.outbox_id)

    await _service(store, FakeGoalScheduler()).resume_generation(generation)



@pytest.mark.asyncio
async def test_resume_generation_replays_submitted_init_before_boundary_i(
    tmp_path,
) -> None:
    db_path = tmp_path / "init-recovery.db"
    store = ThreadStore(db_path)
    await store.ensure_session(
        "main-init-recovery",
        "/workspace",
        profile="goal",
        profile_snapshot=_profile_snapshot(),
    )
    spec = GoalSpec(
        objective="recover init",
        acceptance_condition="boundary I exists",
        generation="gen-init-recovery",
    )
    snapshot = GoalSpecSnapshot.from_spec(
        spec,
        parent_session_id="main-init-recovery",
        parent_thread_id="parent-init-recovery",
        workspace="/workspace",
        profile_snapshot=_profile_snapshot().model_dump(mode="json"),
    )
    record = GoalProtocolRecord.submitted(
        protocol_id="protocol-init-before-boundary",
        parent_session_id="main-init-recovery",
        generation=spec.generation,
        phase="init",
        attempt_number=0,
        turn_id="turn-init-before-boundary",
        session_id="main-init-recovery",
        payload=snapshot,
    )
    await store.submit_goal_protocol(record)
    scheduler = FakeGoalScheduler()

    status = await _service(store, scheduler).resume_generation(spec.generation)

    assert status is not None
    assert scheduler.calls == []
    binding = await store.get_goal_generation(spec.generation)
    assert binding is not None
    assert binding.main_session_id == "main-init-recovery"
    assert binding.goal_thread_id == "goal:parent-init-recovery:gen-init-recovery"
    assert await store.get_session(binding.work_session_id) is not None
    assert await store.get_session(binding.evaluator_session_id) is not None
    protocols = await store.list_goal_protocols(spec.generation)
    assert [(item.sequence_number, item.status) for item in protocols] == [(0, "projected")]
    pending = await store.list_pending_outbox(binding.goal_thread_id)
    assert len(pending) == 1
    assert pending[0].payload["sequence_number"] == 1


@pytest.mark.asyncio
async def test_generation_recovery_lease_is_single_owner_and_expires(tmp_path) -> None:
    db_path = tmp_path / "generation-lease.db"
    first = ThreadStore(db_path)
    second = ThreadStore(db_path)

    assert await first.acquire_goal_generation_lease(
        "gen-lease", "worker-a", lease_seconds=60
    ) is True
    assert await second.acquire_goal_generation_lease(
        "gen-lease", "worker-b", lease_seconds=60
    ) is False
    assert await first.release_goal_generation_lease("gen-lease", "worker-a") is True
    assert await second.acquire_goal_generation_lease(
        "gen-lease", "worker-b", lease_seconds=60
    ) is True

    assert await second.release_goal_generation_lease("gen-lease", "worker-b") is True
    assert await first.acquire_goal_generation_lease(
        "gen-expired", "worker-a", lease_seconds=-1
    ) is True
    assert await second.acquire_goal_generation_lease(
        "gen-expired", "worker-b", lease_seconds=60
    ) is True
    assert await first.release_goal_generation_lease("gen-expired", "worker-a") is False


@pytest.mark.asyncio
async def test_resume_generation_does_not_acquire_lease_for_terminal_generation(
    store: ThreadStore,
) -> None:
    generation = await _initialize(store)
    thread_id = "goal:parent-recovery:gen-recovery"
    for item in await store.list_pending_outbox(thread_id):
        await store.ack_outbox(item.outbox_id)
    loaded = await store.load(thread_id)
    assert loaded is not None
    terminal_state = loaded.state.model_copy(update={"lifecycle": LifecycleState.COMPLETED})
    await store.save_state(
        loaded.thread.thread_id,
        terminal_state,
        expected_state_version=loaded.state_version,
    )

    acquire_calls = 0
    original_acquire = store.acquire_goal_generation_lease

    async def counted_acquire(*args, **kwargs):
        nonlocal acquire_calls
        acquire_calls += 1
        return await original_acquire(*args, **kwargs)

    store.acquire_goal_generation_lease = counted_acquire
    scheduler = FakeGoalScheduler()
    status = await _service(store, scheduler).resume_generation(generation)

    assert status is not None
    assert status.active is False
    assert scheduler.calls == []
    assert acquire_calls == 0
    assert await store.get_goal_generation_lease(generation) is None
    assert await store.list_pending_outbox(thread_id) == []
