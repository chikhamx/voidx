from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import AgentThread
from voidx.agent.loop.manager import LoopManager
from voidx.agent.loop.prompt_source import PromptSource
from voidx.agent.loop.scheduler import LoopRuntimeScheduler
from voidx.memory.thread_store import ThreadStore


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
    assert result.decision.outcome == "completed"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.user_text == "check build"
    assert request.display_text == "[loop] check build"
    assert request.thread.thread_id == "loop:session-1:active"
    assert request.thread.session_id == "session-1"
    assert request.context.thread_id == "loop:session-1:active"
    assert request.context.session_id == "session-1"
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
    assert policy.allows("loop_update") is True
    assert policy.allows("read") is True
    assert policy.allows("schedule_wakeup") is False
    assert policy.allows("clarify") is False
    assert policy.allows("checkpoint") is False
    assert policy.allows("agent") is False
    assert policy.allows("bash") is False




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
async def test_loop_update_decision_overrides_default_completed(tmp_path) -> None:
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
    assert runtime.requests[0].user_text == "right prompt"
    other_outbox = await store.claim_next_outbox(lease_owner="other-worker", lease_seconds=60)
    assert other_outbox is not None
    assert other_outbox.thread_id == "other"

@pytest.mark.asyncio
async def test_loop_manager_uses_runtime_scheduler_instead_of_synthetic_turn(tmp_path) -> None:
    class FakeHost:
        async def forbidden_legacy_turn(self, *_args, **_kwargs):
            raise AssertionError("synthetic turn should not be used when scheduler is bound")

    class FakeScheduler:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None, str | None]] = []
            self.called = asyncio.Event()

        async def run_prompt(self, prompt: str, *, display_text: str | None, session_id: str | None):
            self.calls.append((prompt, display_text, session_id))
            self.called.set()

    idle = asyncio.Event()
    idle.set()
    scheduler = FakeScheduler()
    manager = LoopManager(
        FakeHost(),
        idle_event=idle,
        workspace=str(tmp_path),
        runtime_scheduler=scheduler,
    )

    manager.start(PromptSource.from_raw("check build"), None, session_id="session-1")
    await asyncio.wait_for(scheduler.called.wait(), timeout=1)
    await manager.cleanup()

    assert scheduler.calls == [("check build", "[loop] check build", "session-1")]
