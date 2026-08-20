from __future__ import annotations

import pytest

from voidx.agent.application.coding_service import CODING_PROFILE
from voidx.agent.domain.automation.goal import GOAL_PROFILE, GoalSpec, GoalState
from voidx.agent.domain.thread import AgentThread, AgentThreadState, LifecycleState, RuntimeDecision
from voidx.agent.application.automation.goal.scheduler import GoalRuntimeScheduler
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


class FakeGoalRunnerRuntime:
    def __init__(self) -> None:
        self.seen_frames = []

    async def run_turn(self, *, thread, profile, input_frame):
        self.seen_frames.append((thread, profile, input_frame))
        return RuntimeDecision(outcome="completed", summary="done")


@pytest.mark.asyncio
async def test_goal_scheduler_enqueues_and_dispatches_goal_prompt(tmp_path) -> None:
    store = ThreadStore()
    runtime = FakeGoalRunnerRuntime()
    scheduler = GoalRuntimeScheduler(store=store, runtime=runtime, workspace=str(tmp_path))
    spec = GoalSpec(objective="ship", acceptance_condition="tests pass", generation="run-1")
    state = GoalState.from_spec(spec, run_id="run-1")
    await store.create_thread(
        AgentThread(
            thread_id=spec.goal_thread_id("parent-1"),
            session_id=spec.goal_session_id("parent-1"),
            parent_thread_id="parent-1",
        ),
        profile=GOAL_PROFILE,
        state=AgentThreadState(
            thread_id=spec.goal_thread_id("parent-1"),
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
    )

    result = await scheduler.run_goal("parent-1", spec)

    assert result is not None
    assert runtime.seen_frames
    _, _, frame = runtime.seen_frames[0]
    assert frame["kind"] == "goal_prompt"
    assert frame["spec"]["objective"] == "ship"
    assert frame["goal_state"]["attempt_count"] == 0


@pytest.mark.asyncio
async def test_goal_scheduler_pump_dispatches_only_registered_goal_threads(tmp_path) -> None:
    store = ThreadStore()
    runtime = FakeGoalRunnerRuntime()
    scheduler = GoalRuntimeScheduler(store=store, runtime=runtime, workspace=str(tmp_path))
    spec = GoalSpec(objective="ship", acceptance_condition="tests pass", generation="run-1")
    state = GoalState.from_spec(spec, run_id="run-1")
    await store.create_thread(
        AgentThread(thread_id=spec.goal_thread_id("parent-1"), session_id=spec.goal_session_id("parent-1")),
        profile=GOAL_PROFILE,
        state=AgentThreadState(
            thread_id=spec.goal_thread_id("parent-1"),
            lifecycle=LifecycleState.WAITING,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
    )
    await store.enqueue_outbox(
        thread_id=spec.goal_thread_id("parent-1"),
        kind="wakeup",
        payload={"spec": spec.model_dump(mode="json"), "goal_state": state.model_dump(mode="json")},
        expected_state_version=(await store.load(spec.goal_thread_id("parent-1"))).state_version,
    )

    scheduler.register_goal_thread("goal:other:run-1")
    assert await scheduler._dispatch_next_wakeup() is None
    scheduler.register_goal_thread(spec.goal_thread_id("parent-1"))
    assert await scheduler._dispatch_next_wakeup() is not None


@pytest.mark.asyncio
async def test_goal_scheduler_new_instance_dispatches_durable_goal_wakeup(tmp_path) -> None:
    store = ThreadStore()
    spec = GoalSpec(objective="recover", acceptance_condition="done", generation="run-1")
    state = GoalState.from_spec(spec, run_id="run-1")
    await store.create_thread(
        AgentThread(thread_id=spec.goal_thread_id("parent-1"), session_id=spec.goal_session_id("parent-1")),
        profile=GOAL_PROFILE,
        state=AgentThreadState(
            thread_id=spec.goal_thread_id("parent-1"),
            lifecycle=LifecycleState.WAITING,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
    )
    loaded = await store.load(spec.goal_thread_id("parent-1"))
    assert loaded is not None
    await store.enqueue_outbox(
        thread_id=loaded.thread.thread_id,
        kind="wakeup",
        payload={"spec": spec.model_dump(mode="json"), "goal_state": state.model_dump(mode="json")},
        expected_state_version=loaded.state_version,
    )
    runtime = FakeGoalRunnerRuntime()
    recovered_scheduler = GoalRuntimeScheduler(store=store, runtime=runtime, workspace=str(tmp_path))

    result = await recovered_scheduler._dispatch_next_wakeup()

    assert result is not None
    assert runtime.seen_frames[0][0].thread_id == spec.goal_thread_id("parent-1")


@pytest.mark.asyncio
async def test_goal_scheduler_durable_wakeup_is_not_starved_by_earlier_non_goal_wakeup(tmp_path) -> None:
    store = ThreadStore()
    non_goal_thread = "coding-thread"
    await store.create_thread(
        AgentThread(thread_id=non_goal_thread, session_id="coding-session"),
        profile=CODING_PROFILE,
        state=AgentThreadState(thread_id=non_goal_thread, lifecycle=LifecycleState.WAITING),
    )
    non_goal_loaded = await store.load(non_goal_thread)
    assert non_goal_loaded is not None
    non_goal_outbox = await store.enqueue_outbox(
        thread_id=non_goal_thread,
        kind="wakeup",
        payload={"prompt": "not goal"},
        expected_state_version=non_goal_loaded.state_version,
    )
    spec = GoalSpec(objective="recover", acceptance_condition="done", generation="run-1")
    state = GoalState.from_spec(spec, run_id="run-1")
    await store.create_thread(
        AgentThread(thread_id=spec.goal_thread_id("parent-1"), session_id=spec.goal_session_id("parent-1")),
        profile=GOAL_PROFILE,
        state=AgentThreadState(
            thread_id=spec.goal_thread_id("parent-1"),
            lifecycle=LifecycleState.WAITING,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
    )
    goal_loaded = await store.load(spec.goal_thread_id("parent-1"))
    assert goal_loaded is not None
    await store.enqueue_outbox(
        thread_id=goal_loaded.thread.thread_id,
        kind="wakeup",
        payload={"spec": spec.model_dump(mode="json"), "goal_state": state.model_dump(mode="json")},
        expected_state_version=goal_loaded.state_version,
    )
    runtime = FakeGoalRunnerRuntime()
    scheduler = GoalRuntimeScheduler(store=store, runtime=runtime, workspace=str(tmp_path))

    result = await scheduler._dispatch_next_wakeup()

    assert result is not None
    assert result.thread_id == spec.goal_thread_id("parent-1")
    remaining = await store.claim_next_outbox(lease_owner="coding", lease_seconds=60, kind="wakeup")
    assert remaining is not None
    assert remaining.outbox_id == non_goal_outbox.outbox_id


class RecordingGoalEvents:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def publish_message(self, message: str) -> None:
        self.messages.append(message)

    def start_turn(self, text: str) -> None:
        return None

    def end_turn(self) -> None:
        return None

    def cancel_turn(self) -> None:
        return None

    def fail_turn(self, message: str) -> None:
        return None

    def show_loop_waiting(self, wakeup_at: float) -> None:
        return None

    def clear_loop_waiting(self) -> None:
        return None


@pytest.mark.asyncio
async def test_goal_scheduler_notifies_when_attempt_needs_user(tmp_path) -> None:
    store = ThreadStore()
    runtime = FakeGoalRunnerRuntime()
    runtime.run_turn = lambda **kwargs: None

    async def needs_user(**kwargs):
        runtime.seen_frames.append((kwargs["thread"], kwargs["profile"], kwargs["input_frame"]))
        return RuntimeDecision(
            outcome="needs_user",
            summary="User review required.",
            reason="Acceptance criteria are ambiguous",
        )

    runtime.run_turn = needs_user
    events = RecordingGoalEvents()
    scheduler = GoalRuntimeScheduler(
        store=store,
        runtime=runtime,
        workspace=str(tmp_path),
        events=events,
    )
    spec = GoalSpec(
        objective="ship",
        acceptance_condition="tests pass",
        generation="needs-user-run",
    )
    state = GoalState.from_spec(spec, run_id="needs-user-run")
    await store.create_thread(
        AgentThread(
            thread_id=spec.goal_thread_id("parent-needs-user"),
            session_id=spec.goal_session_id("parent-needs-user"),
            parent_thread_id="parent-needs-user",
        ),
        profile=GOAL_PROFILE,
        state=AgentThreadState(
            thread_id=spec.goal_thread_id("parent-needs-user"),
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
    )

    result = await scheduler.run_goal("parent-needs-user", spec)

    assert result is not None
    assert result.decision.outcome == "needs_user"
    assert events.messages == [
        "Automation paused for user input: Acceptance criteria are ambiguous"
    ]


@pytest.mark.asyncio
async def test_goal_scheduler_new_instance_owns_custom_goal_profile_by_protocol(
    tmp_path,
) -> None:
    store = ThreadStore()
    spec = GoalSpec(
        objective="recover custom goal",
        acceptance_condition="done",
        generation="run-custom",
    )
    state = GoalState.from_spec(spec, run_id="run-custom")
    custom_goal = GOAL_PROFILE.model_copy(
        update={"profile_id": "my-goal", "name": "My Goal"}
    )
    await store.create_thread(
        AgentThread(
            thread_id=spec.goal_thread_id("parent-custom"),
            session_id=spec.goal_session_id("parent-custom"),
        ),
        profile=custom_goal,
        state=AgentThreadState(
            thread_id=spec.goal_thread_id("parent-custom"),
            lifecycle=LifecycleState.WAITING,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
    )
    loaded = await store.load(spec.goal_thread_id("parent-custom"))
    assert loaded is not None
    await store.enqueue_outbox(
        thread_id=loaded.thread.thread_id,
        kind="wakeup",
        payload={
            "spec": spec.model_dump(mode="json"),
            "goal_state": state.model_dump(mode="json"),
        },
        expected_state_version=loaded.state_version,
    )
    runtime = FakeGoalRunnerRuntime()
    recovered = GoalRuntimeScheduler(
        store=store,
        runtime=runtime,
        workspace=str(tmp_path),
    )

    result = await recovered._dispatch_next_wakeup()

    assert result is not None
    assert runtime.seen_frames[0][1].runtime_profile.profile_id == "my-goal"
