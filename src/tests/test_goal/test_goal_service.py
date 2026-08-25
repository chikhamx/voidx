from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.application.agent_registry import AgentRegistry
from voidx.agent.application.automation.goal.goal_service import GoalService
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.domain.automation.goal import (
    GoalProtocolRecord,
    GoalSpec,
    GoalSpecSnapshot,
    GoalState,
)
from voidx.agent.domain.thread import AgentThread, LifecycleState


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    real_init = ThreadStore.__init__
    monkeypatch.setattr(
        ThreadStore,
        "__init__",
        lambda self, db_path=None: real_init(
            self, db_path=db_path if db_path is not None else tmp_path / "store.db"
        ),
    )


@dataclass
class FakeGoalScheduler:
    calls: list[tuple[str, GoalSpec]] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    unregistered: list[str] = field(default_factory=list)
    pump_starts: int = 0

    async def run_goal(self, parent_thread_id: str, spec: GoalSpec):
        self.calls.append((parent_thread_id, spec))

    def register_goal_thread(self, thread_id: str) -> None:
        self.registered.append(thread_id)

    def unregister_goal_thread(self, thread_id: str) -> None:
        self.unregistered.append(thread_id)

    def start_pump(self) -> None:
        self.pump_starts += 1


@pytest.mark.asyncio
async def test_goal_service_start_persists_isolated_goal_state(tmp_path) -> None:
    store = ThreadStore()
    scheduler = FakeGoalScheduler()
    service = GoalService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    status = await service.start(
        "parent-1",
        GoalSpec(objective="ship", acceptance_condition="tests pass", max_attempts=5),
    )

    assert status.active is True
    assert status.goal_thread_id.startswith("goal:parent-1:")
    assert status.objective_summary == "ship"
    assert status.attempt_count == 0
    assert status.max_attempts == 5
    loaded = await store.load(status.goal_thread_id)
    assert loaded is not None
    assert loaded.profile.profile_id == "goal"
    state = GoalState.model_validate(loaded.state.context["goal_run"])
    binding = await store.get_goal_generation(state.generation)
    assert binding is not None
    assert loaded.thread.session_id == binding.work_session_id
    assert state.objective == "ship"
    assert scheduler.registered == [status.goal_thread_id]
    assert scheduler.calls[0][0] == "parent-1"



@pytest.mark.asyncio
async def test_goal_service_start_binds_generation_sessions_atomically(tmp_path) -> None:
    store = ThreadStore()
    await store.ensure_session("parent-1", str(tmp_path), profile="goal")
    scheduler = FakeGoalScheduler()
    service = GoalService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    await service.start(
        "parent-1",
        GoalSpec(objective="ship", acceptance_condition="tests pass"),
    )

    _, spec = scheduler.calls[0]
    binding = await store.get_goal_generation(spec.generation)
    assert binding is not None
    assert binding.main_session_id == "parent-1"
    assert binding.goal_thread_id == spec.goal_thread_id("parent-1")
    assert binding.work_session_id != binding.evaluator_session_id
    assert binding.work_session_id != binding.main_session_id
    assert binding.evaluator_session_id != binding.main_session_id
    assert await store.get_session(binding.work_session_id) is not None
    assert await store.get_session(binding.evaluator_session_id) is not None

    loaded = await store.load(binding.goal_thread_id)
    assert loaded is not None
    assert loaded.thread.session_id == binding.work_session_id
    state = GoalState.model_validate(loaded.state.context["goal_run"])
    assert state.main_session_id == binding.main_session_id
    assert state.work_session_id == binding.work_session_id
    assert state.evaluator_session_id == binding.evaluator_session_id





@pytest.mark.asyncio
async def test_goal_service_start_returns_completed_status_after_synchronous_first_attempt(tmp_path) -> None:
    store = ThreadStore()

    class CompletingScheduler(FakeGoalScheduler):
        async def run_goal(self, parent_thread_id: str, spec: GoalSpec):
            self.calls.append((parent_thread_id, spec))
            loaded = await store.load(spec.goal_thread_id(parent_thread_id))
            assert loaded is not None
            await store.save_state(
                loaded.thread.thread_id,
                loaded.state.model_copy(update={"lifecycle": "completed"}),
                expected_state_version=loaded.state_version,
            )

    service = GoalService(store=store, scheduler=CompletingScheduler(), workspace=str(tmp_path))
    status = await service.start(
        "parent-1", GoalSpec(objective="send messages", acceptance_condition="100 sent")
    )

    assert status.active is False
    assert status.state == "completed"
    assert status.objective_summary == "send messages"


@pytest.mark.asyncio
async def test_goal_service_completed_start_clears_active_spec_before_stop(tmp_path) -> None:
    store = ThreadStore()

    class CompletingScheduler(FakeGoalScheduler):
        async def run_goal(self, parent_thread_id: str, spec: GoalSpec):
            self.calls.append((parent_thread_id, spec))
            loaded = await store.load(spec.goal_thread_id(parent_thread_id))
            assert loaded is not None
            await store.save_state(
                loaded.thread.thread_id,
                loaded.state.model_copy(update={"lifecycle": "completed"}),
                expected_state_version=loaded.state_version,
            )

    scheduler = CompletingScheduler()
    service = GoalService(store=store, scheduler=scheduler, workspace=str(tmp_path))
    await service.start("parent-1", GoalSpec(objective="done", acceptance_condition="done"))

    assert await service.stop("parent-1") is False
    assert scheduler.unregistered == []


@pytest.mark.asyncio
async def test_goal_service_concurrent_starts_keep_latest_generation_active(tmp_path) -> None:
    import asyncio

    store = ThreadStore()
    entered: list[str] = []
    release = asyncio.Event()

    class BlockingScheduler(FakeGoalScheduler):
        async def run_goal(self, parent_thread_id: str, spec: GoalSpec):
            self.calls.append((parent_thread_id, spec))
            entered.append(spec.generation)
            if len(entered) == 1:
                await release.wait()

    scheduler = BlockingScheduler()
    service = GoalService(store=store, scheduler=scheduler, workspace=str(tmp_path))
    first = asyncio.create_task(
        service.start("parent-1", GoalSpec(objective="first", acceptance_condition="first"))
    )
    while len(entered) < 1:
        await asyncio.sleep(0)
    second = asyncio.create_task(
        service.start("parent-1", GoalSpec(objective="second", acceptance_condition="second"))
    )
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await first
    second_status = await second

    assert second_status.objective_summary == "second"
    assert (await service.status("parent-1")).objective_summary == "second"
@pytest.mark.asyncio
async def test_goal_service_status_stop_and_replace_are_independent(tmp_path) -> None:
    store = ThreadStore()
    scheduler = FakeGoalScheduler()
    service = GoalService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    first = await service.start("parent-1", GoalSpec(objective="first", acceptance_condition="done"))
    second = await service.start("parent-1", GoalSpec(objective="second", acceptance_condition="done"))

    assert second.goal_thread_id != first.goal_thread_id
    assert scheduler.unregistered == [first.goal_thread_id]
    assert (await service.status("parent-1")).objective_summary == "second"
    assert await service.stop("parent-1") is True
    assert await service.status("parent-1") is None


@pytest.mark.asyncio
async def test_goal_service_new_instance_recovers_status_and_stop_from_store(tmp_path) -> None:
    store = ThreadStore()
    first_scheduler = FakeGoalScheduler()
    first = GoalService(store=store, scheduler=first_scheduler, workspace=str(tmp_path))
    started = await first.start("parent-1", GoalSpec(objective="recover", acceptance_condition="done"))

    recovered_scheduler = FakeGoalScheduler()
    recovered = GoalService(store=store, scheduler=recovered_scheduler, workspace=str(tmp_path))

    status = await recovered.status("parent-1")

    assert status is not None
    assert status.goal_thread_id == started.goal_thread_id
    assert status.objective_summary == "recover"
    assert recovered_scheduler.registered == [started.goal_thread_id]
    assert recovered_scheduler.pump_starts == 1
    assert await recovered.stop("parent-1") is True
    assert recovered_scheduler.unregistered == [started.goal_thread_id]
    assert await recovered.status("parent-1") is None




@pytest.mark.asyncio
async def test_goal_service_no_notification_without_notifier(tmp_path) -> None:
    store = ThreadStore()

    class CompletingScheduler(FakeGoalScheduler):
        async def run_goal(self, parent_thread_id: str, spec: GoalSpec):
            loaded = await store.load(spec.goal_thread_id(parent_thread_id))
            await store.save_state(
                loaded.thread.thread_id,
                loaded.state.model_copy(update={"lifecycle": "completed"}),
                expected_state_version=loaded.state_version,
            )

    # default: no notifier -> must not raise
    service = GoalService(store=store, scheduler=CompletingScheduler(), workspace=str(tmp_path))
    status = await service.start("parent-1", GoalSpec(objective="ship", acceptance_condition="done"))

    assert status.active is False


@pytest.mark.asyncio
async def test_goal_service_start_reuses_submitted_init_record(tmp_path) -> None:
    store = ThreadStore()
    await store.ensure_session("parent-1", str(tmp_path), profile="goal")
    spec = GoalSpec(
        objective="ship",
        acceptance_condition="tests pass",
        generation="gen-submitted-init",
    )
    snapshot = GoalSpecSnapshot.from_spec(
        spec,
        parent_session_id="parent-1",
        parent_thread_id="parent-1",
        workspace=str(tmp_path),
    )
    submitted = GoalProtocolRecord.submitted(
        protocol_id="submitted-init",
        parent_session_id="parent-1",
        generation=spec.generation,
        phase="init",
        attempt_number=0,
        turn_id="idle-init-turn",
        session_id="parent-1",
        payload=snapshot,
    )
    await store.submit_goal_protocol(submitted)
    scheduler = FakeGoalScheduler()

    status = await GoalService(
        store=store,
        scheduler=scheduler,
        workspace=str(tmp_path),
    ).start("parent-1", spec)

    protocols = await store.list_goal_protocols(spec.generation)
    assert status.active is True
    assert [(record.protocol_id, record.status) for record in protocols] == [
        ("submitted-init", "projected")
    ]
    assert scheduler.calls[0][1].generation == spec.generation
