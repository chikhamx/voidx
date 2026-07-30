from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.application.loop_service import LoopService
from voidx.agent.domain.loop import LoopSpec
from voidx.agent.domain.thread import LifecycleState, RuntimeDecision
from voidx.memory.thread_store import ThreadStore


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    real_init = ThreadStore.__init__
    monkeypatch.setattr(
        ThreadStore,
        "__init__",
        lambda self, db_path=None: real_init(self, db_path=db_path if db_path is not None else tmp_path / "store.db"),
    )


@dataclass
class FakeScheduler:
    calls: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    pump_starts: int = 0
    registered: list[str] = field(default_factory=list)
    unregistered: list[str] = field(default_factory=list)

    async def run_prompt(self, prompt: str, *, display_text: str | None, session_id: str | None, **_kwargs):
        self.calls.append((prompt, display_text, session_id))
        return None

    def start_pump(self) -> None:
        self.pump_starts += 1

    def register_loop_thread(self, thread_id: str) -> None:
        self.registered.append(thread_id)

    def unregister_loop_thread(self, thread_id: str) -> None:
        self.unregistered.append(thread_id)


@pytest.mark.asyncio
async def test_loop_service_start_creates_repository_backed_status(tmp_path) -> None:
    store = ThreadStore()
    scheduler = FakeScheduler()
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    status = await service.start("parent-1", LoopSpec(prompt="check build", interval_seconds=300))

    assert status.active is True
    assert status.parent_thread_id == "parent-1"
    assert status.loop_thread_id.startswith("loop:parent-1:")
    assert status.mode == "fixed"
    assert status.interval_seconds == 300
    assert status.iteration == 0
    assert status.last_summary == ""
    assert scheduler.calls == [("check build", "[loop] check build", "parent-1")]
    assert scheduler.pump_starts == 1

    loaded = await store.load(status.loop_thread_id)
    assert loaded is not None
    assert loaded.thread.parent_thread_id == "parent-1"
    assert loaded.thread.session_id == status.loop_thread_id


@pytest.mark.asyncio
async def test_loop_service_status_and_stop_are_repository_backed(tmp_path) -> None:
    service = LoopService(store=ThreadStore(), scheduler=FakeScheduler(), workspace=str(tmp_path))
    await service.start("parent-1", LoopSpec(prompt="check deploy"))

    status = await service.status("parent-1")
    assert status is not None
    assert status.mode == "dynamic"
    assert status.prompt_summary == "check deploy"

    stopped = await service.stop("parent-1")
    assert stopped is True
    assert await service.status("parent-1") is None


@pytest.mark.asyncio
async def test_loop_service_replaces_active_loop_for_same_parent(tmp_path) -> None:
    store = ThreadStore()
    scheduler = FakeScheduler()
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    first = await service.start("parent-1", LoopSpec(prompt="first"))
    status = await service.start("parent-1", LoopSpec(prompt="second", interval_seconds=60))

    assert status.prompt_summary == "second"
    assert status.mode == "fixed"
    assert [call[0] for call in scheduler.calls] == ["first", "second"]
    assert scheduler.unregistered == [first.loop_thread_id]
    reloaded_first = await store.load(first.loop_thread_id)
    assert reloaded_first is not None
    assert reloaded_first.state.lifecycle is LifecycleState.CANCELLED


@dataclass
class TerminatingScheduler:
    store: ThreadStore
    lifecycle: LifecycleState
    decision: RuntimeDecision

    async def run_prompt(self, prompt: str, *, display_text: str | None, session_id: str | None, spec=None, **_kwargs):
        loop_spec = spec or LoopSpec(prompt=prompt)
        thread_id = loop_spec.loop_thread_id(session_id)
        loaded = await self.store.load(thread_id)
        state = loaded.state.model_copy(
            update={"lifecycle": self.lifecycle, "lifecycle_decision": self.decision}
        )
        await self.store.save_state(thread_id, state, expected_state_version=loaded.state_version)


@pytest.mark.asyncio
async def test_loop_service_start_reports_explicit_completion(tmp_path) -> None:
    store = ThreadStore()
    scheduler = TerminatingScheduler(
        store=store,
        lifecycle=LifecycleState.COMPLETED,
        decision=RuntimeDecision(outcome="completed", summary="task done"),
    )
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    with pytest.raises(RuntimeError, match="loop ended during first iteration: task done"):
        await service.start("parent-1", LoopSpec(prompt="check build"))


@pytest.mark.asyncio
async def test_loop_service_start_reports_iteration_failure(tmp_path) -> None:
    store = ThreadStore()
    scheduler = TerminatingScheduler(
        store=store,
        lifecycle=LifecycleState.FAILED,
        decision=RuntimeDecision(outcome="failed", summary="boom", reason="provider_timeout"),
    )
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    with pytest.raises(RuntimeError, match="loop failed during first iteration: provider_timeout"):
        await service.start("parent-1", LoopSpec(prompt="check build"))


@pytest.mark.asyncio
async def test_loop_service_start_materializes_markdown_reference(tmp_path) -> None:
    (tmp_path / "tasks.md").write_text("snapshot-task-body", encoding="utf-8")
    store = ThreadStore()
    scheduler = FakeScheduler()
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    status = await service.start("parent-1", LoopSpec(prompt="handle @tasks.md"))

    assert status.active is True
    prompt = scheduler.calls[0][0]
    assert "snapshot-task-body" in prompt
    assert status.prompt_summary.startswith("handle @tasks.md")


@pytest.mark.asyncio
async def test_loop_service_start_rejects_missing_reference(tmp_path) -> None:
    from voidx.agent.loop.prompt_materialize import PromptMaterializeError

    service = LoopService(store=ThreadStore(), scheduler=FakeScheduler(), workspace=str(tmp_path))

    with pytest.raises(PromptMaterializeError, match="not found"):
        await service.start("parent-1", LoopSpec(prompt="handle @missing.md"))


@pytest.mark.asyncio
async def test_loop_service_start_creates_missing_parent_session(tmp_path) -> None:
    from voidx.memory.service import get_session

    service = LoopService(store=ThreadStore(), scheduler=FakeScheduler(), workspace=str(tmp_path))

    status = await service.start("default", LoopSpec(prompt="check build"))

    assert status.active is True
    parent = await get_session(status.loop_thread_id)
    assert parent is not None
    assert parent.runtime_profile == "loop"


@pytest.mark.asyncio
async def test_loop_service_start_keeps_existing_parent_session(tmp_path) -> None:
    from voidx.memory.service import create_session, get_session

    existing = await create_session(workspace=str(tmp_path), title="My session", profile="coding")
    service = LoopService(store=ThreadStore(), scheduler=FakeScheduler(), workspace=str(tmp_path))

    status = await service.start(existing.id, LoopSpec(prompt="check build"))

    assert status.active is True
    parent = await get_session(existing.id)
    assert parent is not None
    assert parent.title == "My session"
    assert parent.runtime_profile == "coding"


@pytest.mark.asyncio
async def test_loop_full_dispatch_chain_without_fk_violation(tmp_path) -> None:
    """Regression: loop turn must not hit FOREIGN KEY constraint failed."""
    from voidx.agent.loop.scheduler import LoopRuntimeScheduler
    from voidx.agent.domain.thread import RuntimeDecision as RD

    class FakeRuntime:
        async def run_turn(self, request):
            return None

    async def runner_run_turn(*, thread, profile, input_frame):
        return RD(outcome="continue", summary="iter done", next_delay_seconds=60)

    store = ThreadStore()
    scheduler = LoopRuntimeScheduler(store=store, runtime=FakeRuntime(), workspace=str(tmp_path))
    # Bypass the LLM: drive a decision directly through the real dispatcher/store chain.
    from voidx.agent.loop import scheduler as sched_mod
    original_runner = sched_mod.LoopRuntimeRunner
    sched_mod.LoopRuntimeRunner = type("R", (), {"__init__": lambda self, rt: None, "run_turn": staticmethod(runner_run_turn)})
    try:
        service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))
        status = await service.start("default", LoopSpec(prompt="check build"))
        assert status.active is True

        loaded = await store.load(status.loop_thread_id)
        assert loaded.state.lifecycle is LifecycleState.WAITING
        assert loaded.state.lifecycle_decision is not None
        assert loaded.state.lifecycle_decision.outcome == "continue"
    finally:
        sched_mod.LoopRuntimeRunner = original_runner


@pytest.mark.asyncio
async def test_loop_thread_uses_isolated_session_not_parent(tmp_path) -> None:
    from voidx.memory.service import get_session

    store = ThreadStore()
    service = LoopService(store=store, scheduler=FakeScheduler(), workspace=str(tmp_path))

    status = await service.start("parent-1", LoopSpec(prompt="check build"))

    loaded = await store.load(status.loop_thread_id)
    assert loaded.thread.session_id != "parent-1"
    assert loaded.thread.session_id == status.loop_thread_id
    # The isolated loop session must exist so FK-guarded writes succeed.
    assert await get_session(status.loop_thread_id) is not None


@pytest.mark.asyncio
async def test_loop_context_isolated_from_parent_session_messages(tmp_path) -> None:
    """Regression: loop turn must not load or write parent session messages."""
    from voidx.memory.service import (
        MessageRow,
        count_messages,
        ensure_session,
        load_messages,
        save_message,
    )

    await ensure_session("parent-1", str(tmp_path))

    # Seed the parent session with messages that must stay invisible to the loop.
    await save_message(MessageRow(session_id="parent-1", role="user", content="parent secret context"))
    await save_message(MessageRow(session_id="parent-1", role="assistant", content="parent reply"))

    captured: dict = {}

    class InspectingRuntime:
        async def run_turn(self, request):
            captured["thread"] = request.thread
            captured["context"] = request.context

    from voidx.agent.loop.scheduler import LoopRuntimeRunner, LoopRuntimeScheduler

    store = ThreadStore()
    runtime = InspectingRuntime()
    # Bypass the actual LLM by intercepting the runner's runtime call.
    real_run = LoopRuntimeRunner.run_turn

    async def fake_run_turn(self, *, thread, profile, input_frame):
        captured["thread"] = thread
        return RuntimeDecision(outcome="continue", summary="iter", next_delay_seconds=60)

    LoopRuntimeRunner.run_turn = fake_run_turn
    try:
        scheduler = LoopRuntimeScheduler(store=store, runtime=runtime, workspace=str(tmp_path))
        service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))
        status = await service.start("parent-1", LoopSpec(prompt="check build"))
        assert status.active is True
    finally:
        LoopRuntimeRunner.run_turn = real_run

    thread = captured["thread"]
    # Loop thread must point at its own session, never the parent's.
    assert thread.session_id != "parent-1"
    # Parent message count unchanged: loop wrote nothing into the parent session.
    assert await count_messages("parent-1") == 2
    parent_contents = [m.content for m in await load_messages("parent-1")]
    assert "check build" not in parent_contents


@pytest.mark.asyncio
async def test_loop_restart_migrates_legacy_thread_to_isolated_session(tmp_path) -> None:
    """Legacy loop threads bound to the parent session must be re-pointed on restart."""
    from voidx.agent.domain.thread import AgentThread, AgentThreadState
    from voidx.agent.domain.loop import LOOP_PROFILE
    from voidx.memory.service import ensure_session

    store = ThreadStore()
    # Simulate a pre-fix loop thread whose session_id is the parent session.
    await ensure_session("parent-1", str(tmp_path))
    await store.create_thread(
        AgentThread(
            thread_id="loop:parent-1:active",
            session_id="parent-1",
            parent_thread_id="parent-1",
            workspace=str(tmp_path),
        ),
        profile=LOOP_PROFILE,
        state=AgentThreadState(
            thread_id="loop:parent-1:active",
            lifecycle=LifecycleState.WAITING,
            context={"prompt": "check build", "mode": "dynamic", "interval_seconds": None},
        ),
        resource_scope={"workspace": str(tmp_path)},
    )

    service = LoopService(store=store, scheduler=FakeScheduler(), workspace=str(tmp_path))
    resumed = await service.resume("parent-1")

    assert resumed is not None
    loaded = await store.load("loop:parent-1:active")
    assert loaded.thread.session_id == "loop:parent-1:active"


# ── Stop / restart outbox hygiene ────────────────────────────────────────────


@dataclass
class ContinueRuntime:
    """Submits a far-future continue decision so a wakeup row stays pending."""

    delay: float = 3600.0

    async def run_turn(self, request):
        controller = request.context.loop_controller
        await controller.submit_decision(
            controller.spec_decision(
                outcome="continue", summary="pending", next_delay_seconds=self.delay
            )
        )


@pytest.mark.asyncio
async def test_loop_service_status_counts_completed_iterations(tmp_path) -> None:
    from voidx.agent.loop.scheduler import LoopRuntimeScheduler

    store = ThreadStore()
    scheduler = LoopRuntimeScheduler(store=store, runtime=ContinueRuntime(), workspace=str(tmp_path))
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    status = await service.start("parent-1", LoopSpec(prompt="check"))

    assert status.iteration == 1


async def _pending_outbox(store: ThreadStore, thread_id: str = "loop:parent-1:active") -> list:
    return await store.list_pending_outbox(thread_id)


@pytest.mark.asyncio
async def test_loop_service_stop_discards_pending_wakeup(tmp_path) -> None:
    from voidx.agent.loop.scheduler import LoopRuntimeScheduler

    store = ThreadStore()
    scheduler = LoopRuntimeScheduler(store=store, runtime=ContinueRuntime(), workspace=str(tmp_path))
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    status = await service.start("parent-1", LoopSpec(prompt="check"))
    assert len(await _pending_outbox(store, status.loop_thread_id)) == 1

    stopped = await service.stop("parent-1")

    assert stopped is True
    assert await _pending_outbox(store, status.loop_thread_id) == []


@pytest.mark.asyncio
async def test_loop_service_restart_discards_stale_wakeup_rows(tmp_path) -> None:
    from voidx.agent.loop.scheduler import LoopRuntimeScheduler

    store = ThreadStore()
    scheduler = LoopRuntimeScheduler(store=store, runtime=ContinueRuntime(), workspace=str(tmp_path))
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    first = await service.start("parent-1", LoopSpec(prompt="first"))
    second = await service.start("parent-1", LoopSpec(prompt="second"))

    assert await _pending_outbox(store, first.loop_thread_id) == []
    pending = await _pending_outbox(store, second.loop_thread_id)
    assert len(pending) == 1
    assert pending[0].payload["spec"]["prompt"] == "second"


@dataclass
class PumpTrackingScheduler(FakeScheduler):
    pump_stops: int = 0

    async def stop_pump(self) -> None:
        self.pump_stops += 1


@pytest.mark.asyncio
async def test_loop_service_stop_stops_pump_when_no_loops_remain(tmp_path) -> None:
    scheduler = PumpTrackingScheduler()
    service = LoopService(store=ThreadStore(), scheduler=scheduler, workspace=str(tmp_path))

    await service.start("parent-1", LoopSpec(prompt="check"))
    await service.stop("parent-1")

    assert scheduler.pump_stops == 1


@pytest.mark.asyncio
async def test_loop_service_stop_keeps_pump_while_other_loops_active(tmp_path) -> None:
    scheduler = PumpTrackingScheduler()
    service = LoopService(store=ThreadStore(), scheduler=scheduler, workspace=str(tmp_path))

    await service.start("parent-1", LoopSpec(prompt="one"))
    await service.start("parent-2", LoopSpec(prompt="two"))
    await service.stop("parent-1")

    assert scheduler.pump_stops == 0


# ── Generation: fresh session per start, explicit resume ────────────────────


@pytest.mark.asyncio
async def test_every_start_opens_a_fresh_loop_session(tmp_path) -> None:
    scheduler = FakeScheduler()
    service = LoopService(store=ThreadStore(), scheduler=scheduler, workspace=str(tmp_path))

    first = await service.start("parent-1", LoopSpec(prompt="one"))
    second = await service.start("parent-1", LoopSpec(prompt="two"))

    assert first.loop_thread_id != second.loop_thread_id
    assert first.loop_thread_id.startswith("loop:parent-1:")
    assert second.loop_thread_id.startswith("loop:parent-1:")


@pytest.mark.asyncio
async def test_resume_does_not_reactivate_stopped_loop(tmp_path) -> None:
    scheduler = FakeScheduler()
    service = LoopService(store=ThreadStore(), scheduler=scheduler, workspace=str(tmp_path))

    await service.start("parent-1", LoopSpec(prompt="check"))
    await service.stop("parent-1")
    assert await service.status("parent-1") is None

    resumed = await service.resume("parent-1")

    assert resumed is None
    assert [call[0] for call in scheduler.calls] == ["check"]
    assert scheduler.pump_starts == 1




@pytest.mark.asyncio
async def test_resume_preserves_pending_wakeup_without_immediate_rerun(tmp_path) -> None:
    from voidx.agent.domain.loop import LOOP_PROFILE
    from voidx.agent.domain.thread import AgentThread, AgentThreadState

    store = ThreadStore()
    scheduler = FakeScheduler()
    spec = LoopSpec(prompt="check", interval_seconds=3600, generation="gen-1")
    thread_id = spec.loop_thread_id("parent-1")
    await store.create_thread(
        AgentThread(
            thread_id=thread_id,
            session_id=spec.loop_session_id("parent-1"),
            parent_thread_id="parent-1",
            workspace=str(tmp_path),
        ),
        profile=LOOP_PROFILE,
        state=AgentThreadState(
            thread_id=thread_id,
            lifecycle=LifecycleState.WAITING,
            context={
                "iteration": 1,
                "prompt": spec.prompt,
                "mode": spec.mode.value,
                "interval_seconds": spec.interval_seconds,
                "loop_spec": spec.model_dump(mode="json"),
            },
        ),
        resource_scope={"workspace": str(tmp_path)},
    )
    loaded = await store.load(thread_id)
    assert loaded is not None
    pending = await store.enqueue_outbox(
        thread_id=thread_id,
        kind="wakeup",
        payload={"prompt": spec.prompt, "spec": spec.model_dump(mode="json")},
        expected_state_version=loaded.state_version,
        delay_seconds=3600,
    )

    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))
    resumed = await service.resume("parent-1")

    assert resumed is not None
    assert resumed.loop_thread_id == thread_id
    assert scheduler.calls == []
    assert scheduler.pump_starts == 1
    assert [item.outbox_id for item in await store.list_pending_outbox(thread_id)] == [pending.outbox_id]

@pytest.mark.asyncio
async def test_resume_works_after_process_restart_via_persisted_spec(tmp_path) -> None:
    store = ThreadStore()
    scheduler = FakeScheduler()
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))
    started = await service.start("parent-1", LoopSpec(prompt="check", interval_seconds=60))

    # Simulate a process restart: fresh service instance, same store.
    reloaded = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))
    resumed = await reloaded.resume("parent-1")

    assert resumed is not None
    assert resumed.loop_thread_id == started.loop_thread_id
    assert resumed.mode == "fixed"
    assert resumed.interval_seconds == 60
    assert [call[0] for call in scheduler.calls] == ["check"]


@pytest.mark.asyncio
async def test_resume_without_any_previous_loop_returns_none(tmp_path) -> None:
    service = LoopService(store=ThreadStore(), scheduler=FakeScheduler(), workspace=str(tmp_path))

    assert await service.resume("parent-1") is None
