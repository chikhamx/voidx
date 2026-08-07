from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import AgentThread, LifecycleState, RuntimeDecision
from voidx.agent.application.runtime.dispatcher import RuntimeDispatcher
from voidx.agent.adapters.persistence.thread_repository import ThreadStore


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    real_init = ThreadStore.__init__
    monkeypatch.setattr(
        ThreadStore,
        "__init__",
        lambda self, db_path=None: real_init(self, db_path=db_path if db_path is not None else tmp_path / "store.db"),
    )


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
        lease_owner="seed-worker",
        fencing_token=attempt.fencing_token,
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


# ── Redelivery and conflict safety ──────────────────────────────────────────


async def _seed_committed_wakeup(store: ThreadStore):
    """Commit one continue decision so a wakeup outbox row exists."""
    loaded = await store.load("loop-1")
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
        decision=RuntimeDecision(outcome="continue", summary="seed", next_delay_seconds=0),
        expected_state_version=attempt.state_version,
        lease_owner="seed-worker",
        fencing_token=attempt.fencing_token,
    )
    return committed


async def _make_loop_store() -> ThreadStore:
    store = ThreadStore()
    await store.create_thread(
        AgentThread(thread_id="loop-1"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    return store


@pytest.mark.asyncio
async def test_dispatcher_acks_and_skips_already_committed_attempt() -> None:
    """Crash between commit and ack must not re-run the turn on redelivery."""
    store = await _make_loop_store()
    await _seed_committed_wakeup(store)
    wakeup = await store.claim_next_outbox(lease_owner="w0", lease_seconds=0)
    assert wakeup is not None
    attempt = await store.begin_attempt(
        thread_id="loop-1",
        source_outbox_id=wakeup.outbox_id,
        input_frame={"kind": wakeup.kind, **wakeup.payload},
        expected_state_version=wakeup.expected_state_version,
        lease_owner="w0",
        lease_seconds=60,
    )
    await store.commit_decision(
        attempt_id=attempt.attempt_id,
        decision=RuntimeDecision(outcome="completed", summary="done"),
        expected_state_version=attempt.state_version,
        lease_owner="w0",
        fencing_token=attempt.fencing_token,
    )
    runner = FakeRuntimeRunner([RuntimeDecision(outcome="completed", summary="again")])
    dispatcher = RuntimeDispatcher(store=store, runner=runner, lease_owner="worker-a")

    result = await dispatcher.dispatch_once()

    assert result is None
    assert runner.calls == []
    assert await store.claim_next_outbox(lease_owner="w1", lease_seconds=60) is None


@pytest.mark.asyncio
async def test_dispatcher_acks_stale_outbox_on_state_conflict() -> None:
    """Outbox rows for a superseded state version are poison: ack and skip."""
    store = await _make_loop_store()
    loaded = await store.load("loop-1")
    await store.enqueue_outbox(
        thread_id="loop-1",
        kind="wakeup",
        payload={"prompt": "old generation"},
        expected_state_version=loaded.state_version,
    )
    cancelled = loaded.state.model_copy(update={"lifecycle": LifecycleState.CANCELLED})
    await store.save_state("loop-1", cancelled, expected_state_version=loaded.state_version)
    runner = FakeRuntimeRunner([RuntimeDecision(outcome="completed", summary="x")])
    dispatcher = RuntimeDispatcher(store=store, runner=runner, lease_owner="worker-a")

    result = await dispatcher.dispatch_once()

    assert result is None
    assert runner.calls == []
    assert await store.claim_next_outbox(lease_owner="w1", lease_seconds=60) is None


@dataclass
class _CancellingRunner:
    """Simulates a user /loop stop landing while the model turn is in flight."""

    store: ThreadStore
    calls: list = field(default_factory=list)

    async def run_turn(self, *, thread, profile, input_frame):
        self.calls.append(input_frame)
        loaded = await self.store.load(thread.thread_id)
        cancelled = loaded.state.model_copy(update={"lifecycle": LifecycleState.CANCELLED})
        await self.store.save_state(
            thread.thread_id, cancelled, expected_state_version=loaded.state_version
        )
        return RuntimeDecision(outcome="continue", summary="late", next_delay_seconds=0)


@dataclass
class _FailingRunner:
    error: Exception = RuntimeError("runner failed")

    async def run_turn(self, *, thread, profile, input_frame):
        raise self.error


@pytest.mark.asyncio
async def test_dispatcher_marks_user_review_when_runner_fails_after_side_effect_start():
    store = await _make_loop_store()
    await _seed_committed_wakeup(store)
    dispatcher = RuntimeDispatcher(
        store=store,
        runner=_FailingRunner(),
        lease_owner="worker-a",
    )

    result = await dispatcher.dispatch_once()

    assert result is None
    loaded = await store.load("loop-1")
    assert loaded is not None
    assert loaded.state.lifecycle is LifecycleState.NEEDS_USER
    assert loaded.state.lifecycle_decision is not None
    assert "runner failed" in loaded.state.lifecycle_decision.reason
    assert await store.claim_next_outbox(lease_owner="worker-b", lease_seconds=60) is None



class _NeedsUserWriteFailingStore(ThreadStore):
    def __init__(self, db_path=None):
        super().__init__(db_path=db_path)
        self.acked_outbox_ids = []

    async def set_needs_user_for_attempt(self, *args, **kwargs):
        raise RuntimeError("needs_user write failed")

    async def ack_outbox(self, outbox_id):
        self.acked_outbox_ids.append(outbox_id)
        await super().ack_outbox(outbox_id)


@pytest.mark.asyncio
async def test_dispatcher_does_not_ack_when_needs_user_persistence_fails():
    store = _NeedsUserWriteFailingStore()
    await store.create_thread(
        AgentThread(thread_id="loop-1"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    await _seed_committed_wakeup(store)
    dispatcher = RuntimeDispatcher(
        store=store,
        runner=_FailingRunner(),
        lease_owner="worker-a",
    )

    with pytest.raises(RuntimeError, match="needs_user write failed"):
        await dispatcher.dispatch_once()

    assert store.acked_outbox_ids == []
    assert await store.claim_next_outbox(lease_owner="worker-b", lease_seconds=60) is not None
@pytest.mark.asyncio
async def test_dispatcher_acks_outbox_when_commit_conflicts_after_stop() -> None:
    store = await _make_loop_store()
    await _seed_committed_wakeup(store)
    runner = _CancellingRunner(store)
    dispatcher = RuntimeDispatcher(store=store, runner=runner, lease_owner="worker-a")

    result = await dispatcher.dispatch_once()

    assert result is None
    assert len(runner.calls) == 1
    assert await store.claim_next_outbox(lease_owner="w1", lease_seconds=60) is None
    reloaded = await store.load("loop-1")
    assert reloaded.state.lifecycle is LifecycleState.CANCELLED



@dataclass
class _BlockingRunner:
    calls: int = 0
    first_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_first: asyncio.Event = field(default_factory=asyncio.Event)

    async def run_turn(self, *, thread, profile, input_frame):
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
            await self.release_first.wait()
        return RuntimeDecision(outcome="continue", summary=f"call {self.calls}", next_delay_seconds=60)


@pytest.mark.asyncio
async def test_dispatcher_does_not_rerun_in_flight_attempt_after_outbox_lease_expires() -> None:
    store = await _make_loop_store()
    await _seed_committed_wakeup(store)
    runner = _BlockingRunner()
    first = RuntimeDispatcher(
        store=store,
        runner=runner,
        lease_owner="worker-a",
        lease_seconds=0.01,
        claim_kind="wakeup",
    )
    second = RuntimeDispatcher(
        store=store,
        runner=runner,
        lease_owner="worker-b",
        lease_seconds=0.01,
        claim_kind="wakeup",
    )

    first_task = asyncio.create_task(first.dispatch_once())
    await runner.first_started.wait()
    await asyncio.sleep(0.03)
    second_result = await second.dispatch_once()
    runner.release_first.set()
    first_result = await first_task

    assert second_result is None
    assert first_result is not None
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_claim_next_outbox_filters_by_kind() -> None:
    store = await _make_loop_store()
    loaded = await store.load("loop-1")
    await store.enqueue_outbox(
        thread_id="loop-1",
        kind="loop_prompt",
        payload={"prompt": "manual"},
        expected_state_version=loaded.state_version,
    )
    loaded = await store.load("loop-1")
    await store.enqueue_outbox(
        thread_id="loop-1",
        kind="wakeup",
        payload={"prompt": "due"},
        expected_state_version=loaded.state_version,
    )

    wakeup = await store.claim_next_outbox(kind="wakeup", lease_owner="w", lease_seconds=60)
    any_kind = await store.claim_next_outbox(lease_owner="w", lease_seconds=60)

    assert wakeup is not None and wakeup.kind == "wakeup"
    assert any_kind is not None and any_kind.kind == "loop_prompt"


@pytest.mark.asyncio
async def test_latest_thread_id_with_prefix_returns_newest() -> None:
    store = ThreadStore()
    profile = RuntimeProfile(profile_id="loop", revision=1, name="Loop")
    await store.create_thread(AgentThread(thread_id="loop:p:20260728-01"), profile=profile)
    await store.create_thread(AgentThread(thread_id="loop:p:20260728-02"), profile=profile)
    await store.create_thread(AgentThread(thread_id="loop:other:01"), profile=profile)

    assert await store.latest_thread_id_with_prefix("loop:p:") == "loop:p:20260728-02"
    assert await store.latest_thread_id_with_prefix("loop:missing:") is None


@pytest.mark.asyncio
async def test_discard_pending_outbox_prefix_only_touches_matching_threads() -> None:
    store = await _make_loop_store()
    loaded = await store.load("loop-1")
    await store.enqueue_outbox(
        thread_id="loop-1", kind="wakeup", payload={},
        expected_state_version=loaded.state_version, delay_seconds=3600,
    )
    profile = RuntimeProfile(profile_id="loop", revision=1, name="Loop")
    await store.create_thread(AgentThread(thread_id="loop-2"), profile=profile)
    loaded2 = await store.load("loop-2")
    await store.enqueue_outbox(
        thread_id="loop-2", kind="wakeup", payload={},
        expected_state_version=loaded2.state_version, delay_seconds=3600,
    )

    discarded = await store.discard_pending_outbox_prefix("loop-1")

    assert discarded == 1
    assert await store.list_pending_outbox("loop-1") == []
    assert len(await store.list_pending_outbox("loop-2")) == 1


@dataclass
class _SlowRunner:
    delay: float
    decision: RuntimeDecision

    async def run_turn(self, *, thread, profile, input_frame):
        await asyncio.sleep(self.delay)
        return self.decision


@pytest.mark.asyncio
async def test_dispatcher_renews_short_lease_during_long_turn() -> None:
    store = await _make_loop_store()
    await _seed_committed_wakeup(store)
    runner = _SlowRunner(0.2, RuntimeDecision(outcome="completed", summary="done"))
    dispatcher = RuntimeDispatcher(
        store=store,
        runner=runner,
        lease_owner="worker-a",
        lease_seconds=0.06,
    )

    result = await dispatcher.dispatch_once()

    assert result is not None
    loaded = await store.load("loop-1")
    assert loaded is not None
    assert loaded.state.lifecycle is LifecycleState.COMPLETED
