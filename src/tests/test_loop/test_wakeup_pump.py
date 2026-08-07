"""Due continue wakeups re-run the loop prompt automatically via the scheduler pump."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from voidx.agent.application.automation.loop.scheduler import LoopRuntimeScheduler
from voidx.agent.adapters.persistence.thread_repository import ThreadStore


@dataclass
class ContinueThenCompleteRuntime:
    requests: list = field(default_factory=list)

    async def run_turn(self, request):
        self.requests.append(request)
        controller = request.context.loop_controller
        if len(self.requests) == 1:
            await controller.submit_decision(
                controller.spec_decision(outcome="continue", summary="again", next_delay_seconds=0)
            )
            return
        await controller.submit_decision(
            controller.spec_decision(outcome="completed", summary="done")
        )


async def _wait_for_requests(runtime: ContinueThenCompleteRuntime, count: int) -> None:
    while len(runtime.requests) < count:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_wakeup_payload_carries_prompt_forward(tmp_path) -> None:
    runtime = ContinueThenCompleteRuntime()
    store = ThreadStore(db_path=tmp_path / "store.db")
    scheduler = LoopRuntimeScheduler(
        store=store, runtime=runtime, workspace=str(tmp_path), lease_owner="test-worker"
    )

    await scheduler.run_prompt("check build", display_text="[loop] check build", session_id="s1")

    wakeup = await store.claim_next_outbox(lease_owner="other", lease_seconds=60)
    assert wakeup is not None
    assert wakeup.kind == "wakeup"
    assert wakeup.payload["prompt"] == "check build"
    assert wakeup.payload["display_text"] == "[loop] check build"
    assert wakeup.payload["spec"]["prompt"] == "check build"


@pytest.mark.asyncio
async def test_pump_dispatches_due_wakeup_and_reruns_prompt(tmp_path) -> None:
    runtime = ContinueThenCompleteRuntime()
    scheduler = LoopRuntimeScheduler(
        store=ThreadStore(db_path=tmp_path / "store.db"),
        runtime=runtime,
        workspace=str(tmp_path),
        lease_owner="test-worker",
        pump_poll_seconds=0.01,
    )

    await scheduler.run_prompt("check build", display_text="[loop] check build", session_id="s1")
    scheduler.register_loop_thread("loop:s1:active")
    scheduler.start_pump()
    try:
        await asyncio.wait_for(_wait_for_requests(runtime, 2), timeout=5)
    finally:
        await scheduler.stop_pump()

    assert len(runtime.requests) == 2
    rerun = runtime.requests[1]
    assert rerun.user_text == "Run the next scheduled loop iteration."
    assert "Loop Goal" in rerun.context.runtime_profile.system_prompt
    assert "check build" in rerun.context.runtime_profile.system_prompt
    assert rerun.display_text == "[loop] check build"
    assert rerun.thread.thread_id == "loop:s1:active"


@pytest.mark.asyncio
async def test_pump_stays_idle_without_due_wakeup(tmp_path) -> None:
    runtime = ContinueThenCompleteRuntime()
    scheduler = LoopRuntimeScheduler(
        store=ThreadStore(db_path=tmp_path / "store.db"),
        runtime=runtime,
        workspace=str(tmp_path),
        lease_owner="test-worker",
        pump_poll_seconds=0.01,
    )

    scheduler.start_pump()
    await asyncio.sleep(0.1)
    await scheduler.stop_pump()

    assert runtime.requests == []


@pytest.mark.asyncio
async def test_pump_ignores_undelivered_loop_prompt_rows(tmp_path) -> None:
    """The pump only dispatches due wakeups; loop_prompt rows belong to run_prompt."""
    from voidx.agent.domain.profile import RuntimeProfile
    from voidx.agent.domain.thread import AgentThread

    runtime = ContinueThenCompleteRuntime()
    store = ThreadStore(db_path=tmp_path / "store.db")
    await store.create_thread(
        AgentThread(thread_id="loop:s1:active", session_id="s1"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    loaded = await store.load("loop:s1:active")
    await store.enqueue_outbox(
        thread_id="loop:s1:active",
        kind="loop_prompt",
        payload={"prompt": "stale prompt row"},
        expected_state_version=loaded.state_version,
    )
    scheduler = LoopRuntimeScheduler(
        store=store,
        runtime=runtime,
        workspace=str(tmp_path),
        lease_owner="test-worker",
        pump_poll_seconds=0.01,
    )

    scheduler.start_pump()
    await asyncio.sleep(0.1)
    await scheduler.stop_pump()

    assert runtime.requests == []
