from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.domain.loop import LoopSpec
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import AgentThread
from voidx.agent.loop.scheduler import LoopRuntimeScheduler
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
class FakeRuntime:
    requests: list = field(default_factory=list)

    async def run_turn(self, request):
        self.requests.append(request)
        return None


@pytest.mark.asyncio
async def test_loop_runtime_scheduler_runs_prompt_through_runtime(tmp_path) -> None:
    runtime = FakeRuntime()
    scheduler = LoopRuntimeScheduler(
        store=ThreadStore(),
        runtime=runtime,
        workspace=str(tmp_path),
        lease_owner="test-worker",
    )

    result = await scheduler.run_prompt(
        "check build",
        display_text="[loop] check build",
        session_id="session-1",
    )

    assert result is not None
    assert result.decision.outcome == "continue"
    assert result.decision.next_delay_seconds == 600
    assert result.decision.reason == "no_loop_decision_submitted"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.user_text == "Run the next scheduled loop iteration."
    assert request.persist_user_input is False
    assert "Loop Goal" in request.context.runtime_profile.system_prompt
    assert "check build" in request.context.runtime_profile.system_prompt
    assert request.display_text == "[loop] check build"
    assert request.thread.thread_id == "loop:session-1:active"
    assert request.thread.session_id == "loop:session-1:active"
    assert request.context.thread_id == "loop:session-1:active"
    assert request.context.session_id == "loop:session-1:active"
    assert request.context.runtime_profile.profile_id == "loop"



@pytest.mark.asyncio
async def test_loop_runtime_scheduler_binds_loop_only_tool_policy(tmp_path) -> None:
    runtime = FakeRuntime()
    scheduler = LoopRuntimeScheduler(
        store=ThreadStore(),
        runtime=runtime,
        workspace=str(tmp_path),
        lease_owner="test-worker",
    )

    await scheduler.run_prompt("check build", display_text="[loop] check", session_id="session-1")

    policy = runtime.requests[0].context.tool_policy
    assert policy is not None
    assert policy.allows("loop") is True
    assert policy.allows("read") is True
    assert policy.allows("schedule_wakeup") is False
    assert policy.allows("clarify") is False
    assert policy.allows("checkpoint") is False
    assert policy.allows("agent") is False
    assert policy.allows("bash") is True




@dataclass
class LoopUpdatingRuntime:
    requested_delay: float = 120
    requests: list = field(default_factory=list)

    async def run_turn(self, request):
        self.requests.append(request)
        controller = request.context.loop_controller
        await controller.submit_decision(
            controller.spec_decision(
                outcome="continue",
                summary="scheduled next check",
                next_delay_seconds=self.requested_delay,
            )
        )


@pytest.mark.asyncio
async def test_loop_decision_overrides_default_continuation(tmp_path) -> None:
    runtime = LoopUpdatingRuntime(requested_delay=180)
    scheduler = LoopRuntimeScheduler(
        store=ThreadStore(),
        runtime=runtime,
        workspace=str(tmp_path),
        lease_owner="test-worker",
    )

    result = await scheduler.run_prompt("check build", display_text="[loop] check", session_id="session-1")

    assert result is not None
    assert result.decision.outcome == "continue"
    assert result.decision.summary == "scheduled next check"
    assert result.decision.next_delay_seconds == 180


@pytest.mark.asyncio
async def test_loop_runtime_scheduler_fallback_uses_spec_interval_without_decision(tmp_path) -> None:
    runtime = FakeRuntime()
    scheduler = LoopRuntimeScheduler(
        store=ThreadStore(),
        runtime=runtime,
        workspace=str(tmp_path),
        lease_owner="test-worker",
    )

    result = await scheduler.run_prompt(
        "check build",
        display_text="[loop] check build",
        session_id="session-1",
        spec=LoopSpec(prompt="check build", interval_seconds=120),
    )

    assert result is not None
    assert result.decision.outcome == "continue"
    assert result.decision.next_delay_seconds == 120


@pytest.mark.asyncio
async def test_loop_runtime_scheduler_dispatches_its_own_outbox_when_other_work_is_ready(tmp_path) -> None:
    store = ThreadStore()
    await store.create_thread(
        AgentThread(thread_id="other"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    other = await store.load("other")
    assert other is not None
    await store.enqueue_outbox(
        thread_id="other",
        kind="loop_prompt",
        payload={"prompt": "wrong prompt", "display_text": "[loop] wrong"},
        expected_state_version=other.state_version,
    )
    runtime = FakeRuntime()
    scheduler = LoopRuntimeScheduler(
        store=store,
        runtime=runtime,
        workspace=str(tmp_path),
        lease_owner="test-worker",
    )

    await scheduler.run_prompt(
        "right prompt",
        display_text="[loop] right",
        session_id="session-1",
    )

    assert len(runtime.requests) == 1
    assert runtime.requests[0].user_text == "Run the next scheduled loop iteration."
    assert "right prompt" in runtime.requests[0].context.runtime_profile.system_prompt
    other_outbox = await store.claim_next_outbox(lease_owner="other-worker", lease_seconds=60)
    assert other_outbox is not None
    assert other_outbox.thread_id == "other"


# ── Loop waiting status for UI countdown ─────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_runner_publishes_waiting_record_with_next_wakeup(tmp_path) -> None:
    from voidx.ui.output.events import StatusUpdated, ui_events

    emitted = []

    class _Consumer:
        def handle(self, event):
            emitted.append(event)

    ui_events.start(_Consumer())
    try:
        runtime = LoopUpdatingRuntime(requested_delay=180)
        scheduler = LoopRuntimeScheduler(
            store=ThreadStore(),
            runtime=runtime,
            workspace=str(tmp_path),
            lease_owner="test-worker",
        )
        await scheduler.run_prompt("check build", display_text="[loop] check", session_id="session-1")
        await ui_events.drain()
    finally:
        await ui_events.stop()

    waiting = [
        e for e in emitted
        if isinstance(e, StatusUpdated) and e.status_id == "loop:waiting"
    ]
    assert waiting, "expected a loop:waiting status record"
    assert waiting[-1].display == "record_only"
    import time as _time
    assert abs(float(waiting[-1].detail) - (_time.time() + 180)) < 5


@pytest.mark.asyncio
async def test_pump_skips_wakeup_outside_managed_scope(tmp_path) -> None:
    """A wakeup for a loop this session never started/resumed must not be
    claimed by the pump: it is deferred (lease refreshed) and never run."""
    store = ThreadStore()
    await store.create_thread(
        AgentThread(thread_id="loop:foreign:active"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    await store.enqueue_outbox(
        thread_id="loop:foreign:active",
        kind="wakeup",
        payload={"prompt": "check", "display_text": "[loop] check"},
        expected_state_version=0,
    )

    runtime = FakeRuntime()
    scheduler = LoopRuntimeScheduler(
        store=store,
        runtime=runtime,
        workspace=str(tmp_path),
        lease_owner="test-worker",
        lease_seconds=60,
        session_id="local-session",
    )

    assert await scheduler._dispatch_next_wakeup() is None
    assert runtime.requests == []

    # The foreign wakeup stays pending (not acked) so its owning session can
    # still pick it up later.
    skipped = await store.claim_next_outbox(lease_owner="other-worker", lease_seconds=60)
    assert skipped is not None
    assert skipped.thread_id == "loop:foreign:active"


@pytest.mark.asyncio
async def test_pump_dispatches_registered_loop_wakeup(tmp_path) -> None:
    store = ThreadStore()
    await store.create_thread(
        AgentThread(thread_id="loop:local-session:active"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    await store.enqueue_outbox(
        thread_id="loop:local-session:active",
        kind="wakeup",
        payload={"prompt": "check", "display_text": "[loop] check"},
        expected_state_version=0,
    )

    runtime = FakeRuntime()
    scheduler = LoopRuntimeScheduler(
        store=store,
        runtime=runtime,
        workspace=str(tmp_path),
        lease_owner="test-worker",
        lease_seconds=60,
        session_id="local-session",
    )
    scheduler.register_loop_thread("loop:local-session:active")

    result = await scheduler._dispatch_next_wakeup()

    assert result is not None
    assert result.thread_id == "loop:local-session:active"
    assert len(runtime.requests) == 1


@pytest.mark.asyncio
async def test_pump_without_registered_loops_claims_nothing(tmp_path) -> None:
    """A scheduler with an empty managed set must not consume any wakeup,
    even for threads that look like its own session prefix."""
    store = ThreadStore()
    await store.create_thread(
        AgentThread(thread_id="loop:local-session:active"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    await store.enqueue_outbox(
        thread_id="loop:local-session:active",
        kind="wakeup",
        payload={"prompt": "check"},
        expected_state_version=0,
    )

    scheduler = LoopRuntimeScheduler(
        store=store,
        runtime=FakeRuntime(),
        workspace=str(tmp_path),
        lease_owner="test-worker",
        lease_seconds=60,
        session_id="local-session",
    )

    assert await scheduler._dispatch_next_wakeup() is None
