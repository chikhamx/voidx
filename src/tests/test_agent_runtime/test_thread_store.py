from __future__ import annotations

import asyncio
import warnings

import pytest

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import AgentThread, AgentThreadState, LifecycleState, RuntimeDecision
from voidx.agent.adapters.persistence.thread_repository import ThreadStore, ThreadStateConflict


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    real_init = ThreadStore.__init__
    monkeypatch.setattr(
        ThreadStore,
        "__init__",
        lambda self, db_path=None: real_init(self, db_path=db_path if db_path is not None else tmp_path / "store.db"),
    )


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
async def test_begin_attempt_takes_over_expired_prepared_attempt_with_new_fencing_token():
    store = ThreadStore()
    await store.create_thread(
        AgentThread(thread_id="loop-1"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    loaded = await store.load("loop-1")
    assert loaded is not None
    first = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id="wake-1",
        input_frame={"trigger": "scheduled"},
        expected_state_version=loaded.state_version,
        lease_owner="worker-a",
        lease_seconds=0,
    )

    second = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id="wake-1",
        input_frame={"trigger": "scheduled"},
        expected_state_version=loaded.state_version,
        lease_owner="worker-b",
        lease_seconds=60,
    )

    assert second.attempt_id == first.attempt_id
    assert second.lease_owner == "worker-b"
    assert second.fencing_token == first.fencing_token + 1


@pytest.mark.asyncio
async def test_old_fencing_token_cannot_start_side_effect_after_takeover():
    store = ThreadStore()
    await store.create_thread(
        AgentThread(thread_id="loop-1"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    loaded = await store.load("loop-1")
    assert loaded is not None
    first = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id="wake-1",
        input_frame={},
        expected_state_version=loaded.state_version,
        lease_owner="worker-a",
        lease_seconds=0,
    )
    second = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id="wake-1",
        input_frame={},
        expected_state_version=loaded.state_version,
        lease_owner="worker-b",
        lease_seconds=60,
    )

    assert await store.mark_side_effect_started(
        first.attempt_id,
        lease_owner="worker-a",
        fencing_token=first.fencing_token,
    ) is None
    assert await store.mark_side_effect_started(
        second.attempt_id,
        lease_owner="worker-b",
        fencing_token=second.fencing_token,
    ) is not None
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

    await store.mark_side_effect_started(
        attempt.attempt_id, lease_owner="worker-a", fencing_token=attempt.fencing_token
    )
    committed = await store.commit_decision(
        attempt_id=attempt.attempt_id,
        decision=RuntimeDecision(outcome="continue", summary="done"),
        expected_state_version=attempt.state_version,
        lease_owner="worker-a",
        fencing_token=attempt.fencing_token,
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
        lease_owner="worker-a",
        fencing_token=attempt.fencing_token,
    )
    assert committed.next_outbox_id is not None

    await store.ack_outbox(committed.next_outbox_id)
    await store.ack_outbox(committed.next_outbox_id)
    assert await store.claim_next_outbox(lease_owner="worker-b", lease_seconds=60) is None



@pytest.mark.asyncio
async def test_thread_store_lifecycle_updates_do_not_emit_pydantic_serializer_warnings() -> None:
    store = ThreadStore()
    await store.create_thread(
        AgentThread(thread_id="loop-1"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    loaded = await store.load("loop-1")
    assert loaded is not None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        attempt = await store.begin_attempt(
            thread_id="loop-1",
            source_outbox_id="wake-1",
            input_frame={"trigger": "scheduled"},
            expected_state_version=loaded.state_version,
            lease_owner="worker-a",
            lease_seconds=60,
        )
        assert not [warning for warning in caught if "Pydantic serializer warnings" in str(warning.message)]

    reloaded = await store.load("loop-1")
    assert reloaded is not None
    assert reloaded.state.lifecycle is LifecycleState.RUNNING

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await store.set_needs_user_for_attempt(
            attempt.attempt_id, reason="manual review", lease_owner="worker-a", fencing_token=attempt.fencing_token
        )
        assert not [warning for warning in caught if "Pydantic serializer warnings" in str(warning.message)]

    reloaded = await store.load("loop-1")
    assert reloaded is not None
    assert reloaded.state.lifecycle is LifecycleState.NEEDS_USER


@pytest.mark.asyncio
async def test_thread_store_with_db_path_uses_isolated_database(tmp_path) -> None:
    """ThreadStore(db_path=...) must not touch the global voidx.db — tests and
    tooling need hermetic stores."""
    isolated = tmp_path / "isolated.db"
    store = ThreadStore(db_path=isolated)
    await store.create_thread(
        AgentThread(thread_id="loop-iso"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )

    assert isolated.exists()

    other = ThreadStore(db_path=tmp_path / "other.db")
    assert await other.load("loop-iso") is None


@pytest.mark.asyncio
async def test_commit_decision_atomically_applies_goal_state_patch() -> None:
    from voidx.agent.domain.automation.goal import GOAL_PROFILE, GoalSpec, GoalState
    from voidx.agent.domain.thread import DecisionMetadata

    store = ThreadStore()
    spec = GoalSpec(objective="ship", acceptance_condition="tests pass", generation="run-1")
    goal_state = GoalState.from_spec(spec, run_id="run-id")
    await store.create_thread(
        AgentThread(thread_id="goal:parent:run-1"),
        profile=GOAL_PROFILE,
        state=AgentThreadState(
            thread_id="goal:parent:run-1",
            lifecycle=LifecycleState.READY,
            context={"goal_run": goal_state.model_dump(mode="json")},
        ),
    )
    loaded = await store.load("goal:parent:run-1")
    assert loaded is not None
    attempt = await store.begin_attempt(
        thread_id=loaded.thread.thread_id,
        source_outbox_id="goal-prompt-1",
        input_frame={"spec": spec.model_dump(mode="json")},
        expected_state_version=loaded.state_version,
        lease_owner="worker-a",
        lease_seconds=60,
    )

    decision = RuntimeDecision(
        outcome="continue",
        summary="not yet",
        metadata=DecisionMetadata(
            goal_state_patch={
                "attempt_count": 1,
                "last_evaluator_summary": "missing test evidence",
                "last_progress_key": "implementation",
            }
        ),
    )
    first = await store.commit_decision(
        attempt_id=attempt.attempt_id,
        decision=decision,
        expected_state_version=attempt.state_version,
        lease_owner="worker-a",
        fencing_token=attempt.fencing_token,
    )
    second = await store.commit_decision(
        attempt_id=attempt.attempt_id,
        decision=decision,
        expected_state_version=attempt.state_version,
        lease_owner="worker-a",
        fencing_token=attempt.fencing_token,
    )

    reloaded = await store.load(loaded.thread.thread_id)
    assert reloaded is not None
    committed_goal = GoalState.model_validate(reloaded.state.context["goal_run"])
    assert committed_goal.attempt_count == 1
    assert committed_goal.objective == "ship"
    assert committed_goal.last_evaluator_summary == "missing test evidence"
    assert first.next_outbox_id == second.next_outbox_id
    outbox = await store.claim_next_outbox(lease_owner="worker-b", lease_seconds=60, kind="wakeup")
    assert outbox is not None
    assert outbox.payload["goal_state"]["attempt_count"] == 1
    assert outbox.payload["goal_state"]["last_evaluator_summary"] == "missing test evidence"


@pytest.mark.asyncio
async def test_mark_side_effect_started_is_one_time_execution_claim(tmp_path):
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

    claimed = await store.mark_side_effect_started(
        attempt.attempt_id, lease_owner="worker-a", fencing_token=attempt.fencing_token
    )
    duplicate = await store.mark_side_effect_started(
        attempt.attempt_id, lease_owner="worker-a", fencing_token=attempt.fencing_token
    )

    assert claimed is not None
    assert claimed.side_effect_started is True
    assert duplicate is None


@pytest.mark.asyncio
async def test_renew_attempt_lease_requires_owner_and_fencing_token(tmp_path):
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
        lease_seconds=1,
    )

    assert await store.renew_attempt_lease(
        attempt.attempt_id,
        lease_owner="worker-b",
        fencing_token=attempt.fencing_token,
        lease_seconds=60,
    ) is False
    assert await store.renew_attempt_lease(
        attempt.attempt_id,
        lease_owner="worker-a",
        fencing_token=attempt.fencing_token + 1,
        lease_seconds=60,
    ) is False
    assert await store.renew_attempt_lease(
        attempt.attempt_id,
        lease_owner="worker-a",
        fencing_token=attempt.fencing_token,
        lease_seconds=60,
    ) is True


@pytest.mark.asyncio
async def test_commit_decision_rejects_expired_attempt_lease(tmp_path):
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
        lease_seconds=0.01,
    )
    await asyncio.sleep(0.03)

    with pytest.raises(ThreadStateConflict):
        await store.commit_decision(
            attempt_id=attempt.attempt_id,
            decision=RuntimeDecision(outcome="completed", summary="late"),
            expected_state_version=attempt.state_version,
            lease_owner="worker-a",
            fencing_token=attempt.fencing_token,
        )


@pytest.mark.asyncio
async def test_attempt_mutations_require_matching_lease_fencing(tmp_path):
    store = ThreadStore(db_path=tmp_path / "store.db")
    await store.create_thread(AgentThread(thread_id="loop-1"), profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"))
    loaded = await store.load("loop-1")
    attempt = await store.begin_attempt(
        thread_id="loop-1", source_outbox_id="wake-1", input_frame={},
        expected_state_version=loaded.state_version, lease_owner="worker-a", lease_seconds=60,
    )

    assert await store.mark_side_effect_started(
        attempt.attempt_id, lease_owner="worker-b", fencing_token=attempt.fencing_token,
    ) is None
    with pytest.raises(ThreadStateConflict):
        await store.set_needs_user_for_attempt(
            attempt.attempt_id, reason="review", lease_owner="worker-b", fencing_token=attempt.fencing_token,
        )
    with pytest.raises(TypeError):
        await store.commit_decision(
            attempt_id=attempt.attempt_id,
            decision=RuntimeDecision(outcome="completed", summary="done"),
            expected_state_version=attempt.state_version,
        )
