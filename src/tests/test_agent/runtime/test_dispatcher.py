from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import AgentThread, LifecycleState, RuntimeDecision
from voidx.agent.runtime.dispatcher import RuntimeDispatcher
from voidx.memory.thread_store import ThreadStore


@dataclass
class FakeRuntimeRunner:
    decisions: list[RuntimeDecision]
    calls: list[dict] = field(default_factory=list)

    async def run_turn(self, *, thread, profile, input_frame):
        self.calls.append(
            {"thread_id": thread.thread_id, "profile_id": profile.profile_id, "input_frame": input_frame}
        )
        return self.decisions.pop(0)


@pytest.mark.asyncio
async def test_dispatcher_claims_outbox_runs_runtime_and_commits_decision() -> None:
    store = ThreadStore()
    await store.create_thread(
        AgentThread(thread_id="loop-1"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    loaded = await store.load("loop-1")
    assert loaded is not None
    attempt = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id="seed",
        input_frame={},
        expected_state_version=loaded.state_version,
        lease_owner="seed-worker",
        lease_seconds=60,
    )
    committed = await store.commit_decision(
        attempt_id=attempt.attempt_id,
        decision=RuntimeDecision(outcome="continue", summary="seed"),
        expected_state_version=attempt.state_version,
    )
    assert committed.next_outbox_id is not None
    runner = FakeRuntimeRunner([RuntimeDecision(outcome="completed", summary="done")])
    dispatcher = RuntimeDispatcher(store=store, runner=runner, lease_owner="worker-a")

    result = await dispatcher.dispatch_once()

    assert result is not None
    assert result.decision.outcome == "completed"
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["thread_id"] == "loop-1"
    assert call["profile_id"] == "loop"
    assert call["input_frame"]["kind"] == "wakeup"
    assert call["input_frame"]["decision"]["summary"] == "seed"
    reloaded = await store.load("loop-1")
    assert reloaded is not None
    assert reloaded.state.lifecycle is LifecycleState.COMPLETED


@pytest.mark.asyncio
async def test_dispatcher_returns_none_when_no_outbox_ready() -> None:
    dispatcher = RuntimeDispatcher(store=ThreadStore(), runner=FakeRuntimeRunner([]), lease_owner="worker-a")

    assert await dispatcher.dispatch_once() is None
