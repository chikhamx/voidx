from __future__ import annotations

import pytest

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import AgentThread, LifecycleState, RuntimeDecision
from voidx.agent.runtime.recovery import RuntimeRecoveryWorker
from voidx.memory.thread_store import ThreadStore


@pytest.mark.asyncio
async def test_recovery_moves_side_effect_attempt_to_needs_user(tmp_path) -> None:
    store = ThreadStore(db_path=tmp_path / "store.db")
    await store.create_thread(
        AgentThread(thread_id="loop-1"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    loaded = await store.load("loop-1")
    assert loaded is not None
    attempt = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id="wake-1",
        input_frame={},
        expected_state_version=loaded.state_version,
        lease_owner="worker-a",
        lease_seconds=60,
    )
    await store.mark_side_effect_started(attempt.attempt_id)
    worker = RuntimeRecoveryWorker(store=store)

    result = await worker.recover_attempt(attempt.attempt_id)

    assert result.action == "needs_user"
    reloaded = await store.load("loop-1")
    assert reloaded is not None
    assert reloaded.state.lifecycle is LifecycleState.NEEDS_USER
    assert reloaded.state.lifecycle_decision is not None
    assert "side effect" in reloaded.state.lifecycle_decision.reason


@pytest.mark.asyncio
async def test_recovery_acknowledges_committed_attempt_source_outbox(tmp_path) -> None:
    store = ThreadStore(db_path=tmp_path / "store.db")
    await store.create_thread(
        AgentThread(thread_id="loop-1"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    loaded = await store.load("loop-1")
    assert loaded is not None
    seed_attempt = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id="seed",
        input_frame={},
        expected_state_version=loaded.state_version,
        lease_owner="worker-a",
        lease_seconds=60,
    )
    seed_commit = await store.commit_decision(
        attempt_id=seed_attempt.attempt_id,
        decision=RuntimeDecision(outcome="continue", summary="next"),
        expected_state_version=seed_attempt.state_version,
    )
    assert seed_commit.next_outbox_id is not None
    outbox = await store.claim_next_outbox(lease_owner="worker-b", lease_seconds=0)
    assert outbox is not None
    attempt = await store.begin_attempt(
        thread_id=outbox.thread_id,
        source_outbox_id=outbox.outbox_id,
        input_frame={"kind": outbox.kind},
        expected_state_version=outbox.expected_state_version,
        lease_owner="worker-b",
        lease_seconds=60,
    )
    await store.commit_decision(
        attempt_id=attempt.attempt_id,
        decision=RuntimeDecision(outcome="completed", summary="done"),
        expected_state_version=attempt.state_version,
    )
    worker = RuntimeRecoveryWorker(store=store)

    result = await worker.recover_attempt(attempt.attempt_id)

    assert result.action == "committed"
    assert await store.claim_next_outbox(lease_owner="worker-c", lease_seconds=60) is None
