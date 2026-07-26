from __future__ import annotations

import pytest

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import AgentThread, AgentThreadState, LifecycleState, RuntimeDecision
from voidx.memory.thread_store import ThreadStore, ThreadStateConflict


@pytest.mark.asyncio
async def test_thread_store_creates_and_loads_child_thread() -> None:
    store = ThreadStore()
    class PromptPolicy:
        pass

    profile = RuntimeProfile(
        profile_id="loop",
        revision=1,
        name="Loop",
        system_prompt="watch",
        prompt_policy=PromptPolicy(),
    )
    thread = AgentThread(thread_id="loop-1", session_id="s1", parent_thread_id="parent-1", workspace="/tmp/ws")
    state = AgentThreadState(thread_id="loop-1", lifecycle=LifecycleState.READY)

    await store.create_thread(thread, profile=profile, state=state, resource_scope={"tools": ["read"]})
    loaded = await store.load("loop-1")

    assert loaded is not None
    assert loaded.thread == thread
    assert loaded.profile.profile_id == "loop"
    assert loaded.profile.system_prompt == "watch"
    assert loaded.profile.prompt_policy is None
    assert loaded.state.lifecycle is LifecycleState.READY
    assert loaded.resource_scope == {"tools": ["read"]}


@pytest.mark.asyncio
async def test_thread_store_save_state_uses_optimistic_version() -> None:
    store = ThreadStore()
    thread = AgentThread(thread_id="loop-1")
    profile = RuntimeProfile(profile_id="loop", revision=1, name="Loop")
    await store.create_thread(thread, profile=profile)

    loaded = await store.load("loop-1")
    assert loaded is not None
    next_state = loaded.state.model_copy(update={"lifecycle": LifecycleState.WAITING})

    saved = await store.save_state("loop-1", next_state, expected_state_version=loaded.state_version)

    assert saved.state_version == loaded.state_version + 1
    with pytest.raises(ThreadStateConflict):
        await store.save_state("loop-1", next_state, expected_state_version=loaded.state_version)


@pytest.mark.asyncio
async def test_begin_attempt_is_idempotent_by_source_outbox() -> None:
    store = ThreadStore()
    await store.create_thread(AgentThread(thread_id="loop-1"), profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"))
    loaded = await store.load("loop-1")
    assert loaded is not None

    first = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id="wake-1",
        input_frame={"trigger": "scheduled"},
        expected_state_version=loaded.state_version,
        lease_owner="worker-a",
        lease_seconds=60,
    )
    second = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id="wake-1",
        input_frame={"trigger": "scheduled"},
        expected_state_version=loaded.state_version,
        lease_owner="worker-a",
        lease_seconds=60,
    )

    assert second.attempt_id == first.attempt_id
    assert second.fencing_token == first.fencing_token


@pytest.mark.asyncio
async def test_mark_side_effect_started_and_commit_decision_writes_state_and_outbox() -> None:
    store = ThreadStore()
    await store.create_thread(AgentThread(thread_id="loop-1"), profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"))
    loaded = await store.load("loop-1")
    assert loaded is not None
    attempt = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id="wake-1",
        input_frame={"trigger": "scheduled"},
        expected_state_version=loaded.state_version,
        lease_owner="worker-a",
        lease_seconds=60,
    )

    await store.mark_side_effect_started(attempt.attempt_id)
    committed = await store.commit_decision(
        attempt_id=attempt.attempt_id,
        decision=RuntimeDecision(outcome="continue", summary="done"),
        expected_state_version=attempt.state_version,
    )

    reloaded = await store.load("loop-1")
    assert reloaded is not None
    assert reloaded.state.lifecycle is LifecycleState.WAITING
    assert reloaded.state.lifecycle_decision is not None
    assert reloaded.state.lifecycle_decision.summary == "done"
    assert committed.next_outbox_id is not None
    outbox = await store.claim_next_outbox(lease_owner="worker-b", lease_seconds=60)
    assert outbox is not None
    assert outbox.outbox_id == committed.next_outbox_id
    assert outbox.thread_id == "loop-1"


@pytest.mark.asyncio
async def test_ack_outbox_is_idempotent() -> None:
    store = ThreadStore()
    await store.create_thread(AgentThread(thread_id="loop-1"), profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"))
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
    committed = await store.commit_decision(
        attempt_id=attempt.attempt_id,
        decision=RuntimeDecision(outcome="continue", summary="again"),
        expected_state_version=attempt.state_version,
    )
    assert committed.next_outbox_id is not None

    await store.ack_outbox(committed.next_outbox_id)
    await store.ack_outbox(committed.next_outbox_id)

    assert await store.claim_next_outbox(lease_owner="worker-b", lease_seconds=60) is None
