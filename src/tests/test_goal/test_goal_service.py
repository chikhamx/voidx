from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.application.automation.goal.goal_service import GoalService
from voidx.agent.domain.automation.goal import GoalSpec, GoalState
from voidx.agent.adapters.persistence.thread_repository import ThreadStore


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
    assert loaded.thread.session_id == status.goal_thread_id
    assert GoalState.model_validate(loaded.state.context["goal_run"]).objective == "ship"
    assert scheduler.registered == [status.goal_thread_id]
    assert scheduler.calls[0][0] == "parent-1"




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
async def test_goal_service_notifies_parent_on_terminal_completion(tmp_path) -> None:
    store = ThreadStore()
    notified: list[str] = []

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

    service = GoalService(
        store=store,
        scheduler=CompletingScheduler(),
        workspace=str(tmp_path),
        result_notifier=lambda parent, text: notified.append((parent, text)),
    )

    await service.start("parent-1", GoalSpec(objective="ship", acceptance_condition="tests pass"))

    assert len(notified) == 1
    parent, text = notified[0]
    assert parent == "parent-1"
    assert "ship" in text
    assert "completed" in text.lower() or "finished" in text.lower()


@pytest.mark.asyncio
async def test_goal_service_terminal_notification_only_once(tmp_path) -> None:
    store = ThreadStore()
    notified: list[str] = []

    class CompletingScheduler(FakeGoalScheduler):
        async def run_goal(self, parent_thread_id: str, spec: GoalSpec):
            loaded = await store.load(spec.goal_thread_id(parent_thread_id))
            await store.save_state(
                loaded.thread.thread_id,
                loaded.state.model_copy(update={"lifecycle": "completed"}),
                expected_state_version=loaded.state_version,
            )

    service = GoalService(
        store=store,
        scheduler=CompletingScheduler(),
        workspace=str(tmp_path),
        result_notifier=lambda parent, text: notified.append(text),
    )

    await service.start("parent-1", GoalSpec(objective="ship", acceptance_condition="done"))
    # subsequent status polls must not re-notify
    await service._status("parent-1", include_terminal=True)
    await service._status("parent-1", include_terminal=True)

    assert len(notified) == 1


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
