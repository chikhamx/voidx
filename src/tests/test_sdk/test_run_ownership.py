"""Run owner creation-time budgets and bounded cancellation."""
import asyncio

import pytest

from voidx.agent.application.runtime.run_event_mux import RunEventMux
from voidx.agent.application.runtime.run_supervisor import RunSupervisor, RunResult
from voidx.agent.domain import semantic_events as e


def test_supervisor_owner_reserves_before_coroutine_creation():
    async def run():
        from voidx.agent.application.runtime.semantic_channel import SemanticChannel
        channel = SemanticChannel(session_id="s", thread_id="t", turn_id="u")
        created = []
        class Owner:
            def spawn(self, factory, *, role="execution"):
                raise RuntimeError("Nested producer capacity exhausted")
        async def execute(supervisor):
            with pytest.raises(RuntimeError, match="capacity"):
                supervisor.spawn_factory(lambda: created.append(True))
            return RunResult(completed=e.TurnCompletedPayload(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}))
        async def persist(result):
            pass
        async def cleanup(outcome):
            return ()
        supervisor = RunSupervisor(channel, execute=execute, persist=persist, cleanup=cleanup, owner=Owner())
        await supervisor.wait()
        assert not created
        assert (await channel.completion).terminal
        assert e.decode_event((await channel.completion).terminal).kind == "turn.completed"
    asyncio.run(run())


def owner_type():
    from voidx.agent.application.runtime.run_ownership import RunOwner
    return RunOwner


def test_owner_reserves_renewal_and_pumps_before_factory():
    async def run():
        owner = owner_type()(turns=1, producers=4)
        gate = asyncio.Event()
        first = owner.spawn(gate.wait)
        created = []
        with pytest.raises(RuntimeError, match="capacity"):
            owner.spawn(lambda: created.append(True))
        renewal = owner.spawn(gate.wait, role="renewal")
        pumps = [owner.spawn(gate.wait, role="pump") for _ in range(2)]
        assert owner.producer_count == 4
        assert not created
        await asyncio.wait_for(owner.cancel(), 5)
        assert all(task.done() for task in (first, renewal, *pumps))
        assert owner.producer_count == 0
        assert (await owner.completion).outcome == "cancelled"
    asyncio.run(run())


def test_owner_pump_failure_wakes_empty_stream_without_fake_turn():
    async def run():
        owner = owner_type()(turns=1, producers=4)
        async def fail():
            raise ValueError("secret-token")
        owner.spawn(fail, role="pump")
        with pytest.raises(RuntimeError, match="^Run failed$"):
            await asyncio.wait_for(anext(owner.events()), 5)
        assert owner.producer_count == owner.mux.registered == 0
        assert (await owner.completion).summary == "Run failed"
        await owner.cancel()
    asyncio.run(run())


def test_owner_cancels_full_real_turn_without_consumer():
    async def run():
        owner = owner_type()(turns=1, producers=4, capacity=1)
        entered = asyncio.Event()
        async def execute(supervisor):
            envelope = dict(**supervisor.channel.identity, event_id=__import__('uuid').uuid4(), sequence=1,
                            timestamp=0.0, agent_id=None, parent_tool_call_id=None)
            item = e.ToolStarted(**envelope, payload={"tool_call_id": "t", "name": "read", "arguments": {}})
            await supervisor.channel.publish(item)
            entered.set()
            await supervisor.channel.publish(item)
        async def persist(result):
            pass
        cleaned = []
        async def cleanup(outcome):
            cleaned.append(outcome)
            return ()
        supervisor = await owner.register_turn(session_id="child", thread_id="real", execute=execute,
                                               persist=persist, cleanup=cleanup)
        await entered.wait()
        await asyncio.wait_for(owner.aclose(), 5)
        assert cleaned == ["cancelled"]
        assert owner.producer_count == 0
        assert supervisor.channel._completion.done()
        output = [item async for item in owner.events()]
        assert [item.kind for item in output] == ["tool.started", "turn.cancelled"]
        assert all(item.session_id == "child" for item in output)
        assert not owner._supervisors
        assert owner.mux.registered == 0
    asyncio.run(run())


def test_registered_turn_uses_execution_budget_before_start():
    async def run():
        owner = owner_type()(turns=1, producers=4)
        entered = asyncio.Event()
        async def execute(supervisor):
            entered.set()
            await asyncio.Event().wait()
        async def persist(result):
            pass
        async def cleanup(outcome):
            return ()
        await owner.register_turn(session_id="s", thread_id="t", execute=execute, persist=persist, cleanup=cleanup)
        await entered.wait()
        assert owner.producer_count == 1
        with pytest.raises(RuntimeError, match="capacity"):
            owner.spawn(entered.wait)
        await owner.cancel()
        assert owner.producer_count == 0
    asyncio.run(run())


def test_managed_pump_propagates_failure_to_run():
    async def run():
        from voidx.agent.application.runtime.pump import WakeupPumpMixin
        owner = owner_type()(turns=1, producers=4)
        class Pump(WakeupPumpMixin):
            async def _dispatch_next_wakeup(self):
                raise ValueError("private failure")
        pump = Pump()
        pump._init_pump(lease_owner="x", lease_seconds=1, pump_poll_seconds=.001, owner=owner)
        pump.start_pump()
        with pytest.raises(RuntimeError, match="^Run failed$"):
            await asyncio.wait_for(anext(owner.events()), 5)
        assert pump._pump_task.done()
        assert owner.producer_count == 0
        await owner.cancel()
    asyncio.run(run())


@pytest.mark.asyncio
async def test_dispatcher_renewal_is_owned(tmp_path):
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.application.runtime.dispatcher import RuntimeDispatcher
    from voidx.agent.domain.profile import RuntimeProfile
    from voidx.agent.domain.thread import AgentThread, RuntimeDecision
    store = ThreadStore(tmp_path / "store.db")
    await store.create_thread(AgentThread(thread_id="loop"), profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"))
    loaded = await store.load("loop")
    attempt = await store.begin_attempt(thread_id="loop", source_outbox_id="seed", input_frame={},
                                        expected_state_version=loaded.state_version, lease_owner="seed", lease_seconds=60)
    await store.commit_decision(attempt_id=attempt.attempt_id, decision=RuntimeDecision(outcome="continue", summary="seed"),
                               expected_state_version=attempt.state_version, lease_owner="seed", fencing_token=attempt.fencing_token)
    owner = owner_type()(turns=1, producers=4)
    class Runner:
        async def run_turn(self, **kwargs):
            assert owner._counts["renewal"] == 1
            return RuntimeDecision(outcome="completed", summary="done")
    dispatcher = RuntimeDispatcher(store=store, runner=Runner(), lease_owner="worker", owner=owner)
    result = await dispatcher.dispatch_once()
    assert result.decision.outcome == "completed"
    await owner.cancel()
    assert owner.producer_count == 0


def test_owner_normal_completion_and_started_are_real_turn_events():
    async def run():
        owner = owner_type()(turns=1, producers=4)
        async def execute(supervisor):
            from uuid import uuid4
            await supervisor.channel.publish(e.TurnStarted(
                **supervisor.channel.identity, event_id=uuid4(), sequence=1, timestamp=0.0,
                agent_id=None, parent_tool_call_id=None, payload={"text": "input", "metadata": {}},
            ))
            return RunResult(completed=e.TurnCompletedPayload(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}))
        async def persist(result):
            pass
        async def cleanup(outcome):
            return ()
        await owner.register_turn(session_id="s", thread_id="t", execute=execute, persist=persist, cleanup=cleanup)
        await asyncio.wait_for(owner.finish(), 5)
        items = [item async for item in owner.events()]
        assert [item.kind for item in items] == ["turn.started", "turn.completed"]
        assert (await owner.completion).outcome == "completed"
    asyncio.run(run())


def test_nested_factory_children_cannot_outlive_turn_terminal():
    async def run():
        owner = owner_type()(turns=1, producers=5)
        child_done = asyncio.Event()
        async def child():
            assert owner.producer_count == 2
            await asyncio.sleep(.01)
            child_done.set()
        async def execute(supervisor):
            supervisor.spawn_factory(child)
            return RunResult(completed=e.TurnCompletedPayload(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}))
        async def persist(result):
            assert child_done.is_set()
        async def cleanup(outcome):
            return ()
        supervisor = await owner.register_turn(session_id="s", thread_id="t", execute=execute, persist=persist, cleanup=cleanup)
        await supervisor.wait()
        assert child_done.is_set()
        assert owner.producer_count == 0
        await owner.finish()
    asyncio.run(run())


def test_cancel_tail_budget_released_only_when_consumed():
    async def run():
        from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
        from voidx.tooling.domain.interaction import InteractionRequest
        owner = owner_type()(turns=1, producers=4, capacity=1, interactions=1)
        requested = asyncio.Event()
        coordinators = []
        async def execute(supervisor):
            c = InteractionCoordinator(supervisor.channel, **supervisor.channel.identity, budget=owner.interactions)
            coordinators.append(c)
            requested.set()
            await c.request(InteractionRequest(**supervisor.channel.identity, interaction_id=c.issue_id(),
                                             input_kind="text", purpose="clarify", prompt="Question"))
        async def persist(result):
            pass
        async def cleanup(outcome):
            return await coordinators[0].cleanup(outcome)
        await owner.register_turn(session_id="s", thread_id="t", execute=execute, persist=persist, cleanup=cleanup)
        await requested.wait()
        await asyncio.sleep(0)
        await asyncio.wait_for(owner.cancel(), 5)
        assert owner.interactions.count == 1
        items = [item async for item in owner.events()]
        assert [item.kind for item in items] == ["interaction.required", "interaction.resolved", "turn.cancelled"]
        assert owner.interactions.count == 0
    asyncio.run(run())


def test_nested_registration_infers_parent_and_fails_without_waiting():
    async def run():
        owner = owner_type()(turns=1, producers=5)
        async def persist(result):
            pass
        async def cleanup(outcome):
            return ()
        async def execute(supervisor):
            with pytest.raises(RuntimeError, match="capacity"):
                await asyncio.wait_for(owner.register_turn(session_id="child", thread_id="child-thread",
                    execute=execute, persist=persist, cleanup=cleanup), .1)
            return RunResult(completed=e.TurnCompletedPayload(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}))
        supervisor = await owner.register_turn(session_id="parent", thread_id="parent-thread", execute=execute, persist=persist, cleanup=cleanup)
        await supervisor.wait()
        assert e.decode_event(supervisor.channel._completion.result().terminal).kind == "turn.completed"
        await owner.finish()
    asyncio.run(run())


@pytest.mark.asyncio
async def test_turn_admission_waits_for_execution_capacity_without_creating_work():
    owner = owner_type()(turns=1, producers=4)
    gate = asyncio.Event()
    blocker = owner.spawn(gate.wait)
    entered = asyncio.Event()
    async def execute(supervisor):
        entered.set()
        await asyncio.Event().wait()
    async def persist(result):
        pass
    async def cleanup(outcome):
        return ()
    admission = asyncio.create_task(owner.register_turn(session_id="s", thread_id="t", execute=execute, persist=persist, cleanup=cleanup))
    try:
        await asyncio.sleep(0)
        assert not admission.done(), "ordinary admission must wait, not reject"
        assert not entered.is_set()
        gate.set()
        await blocker
        await asyncio.wait_for(admission, 1)
        assert owner._counts["execution"] == 1
    finally:
        await owner.cancel()
        await asyncio.gather(admission, return_exceptions=True)


@pytest.mark.asyncio
async def test_owned_supervisor_legacy_spawn_cannot_bypass_capacity():
    owner = owner_type()(turns=1, producers=4)
    rejected = []
    async def execute(supervisor):
        coroutine = asyncio.sleep(0)
        try:
            supervisor.spawn(coroutine)
        except RuntimeError:
            rejected.append(True)
        return RunResult(completed=e.TurnCompletedPayload(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}))
    async def persist(result):
        pass
    async def cleanup(outcome):
        return ()
    supervisor = await owner.register_turn(session_id="s", thread_id="t", execute=execute, persist=persist, cleanup=cleanup)
    await supervisor.wait()
    await owner.finish()
    assert rejected == [True]
    assert owner.producer_count == 0


@pytest.mark.asyncio
async def test_normal_finish_stops_idle_pumps_before_completion():
    owner = owner_type()(turns=1, producers=4)
    stopped = asyncio.Event()
    async def pump():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    owner.spawn(pump, role="pump")
    await asyncio.sleep(0)
    try:
        await asyncio.wait_for(owner.finish(), .2)
        assert stopped.is_set()
        assert owner.producer_count == 0
        assert (await owner.completion).outcome == "completed"
        await owner.aclose()
        await owner.aclose()
        assert (await owner.completion).outcome == "completed"
    finally:
        await owner.cancel()


@pytest.mark.asyncio
async def test_turn_waiter_rechecks_producer_budget_after_mux_slot_drains():
    owner = owner_type()(turns=1, producers=4)
    async def execute(supervisor):
        return RunResult(completed=e.TurnCompletedPayload(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}))
    async def persist(result):
        pass
    async def cleanup(outcome):
        return ()
    first = await owner.register_turn(session_id="s", thread_id="first", execute=execute, persist=persist, cleanup=cleanup)
    await first.wait()
    admission = asyncio.create_task(owner.register_turn(session_id="s", thread_id="second", execute=execute, persist=persist, cleanup=cleanup))
    await asyncio.sleep(0)
    gate = asyncio.Event()
    blocker = owner.spawn(gate.wait)
    stream = owner.events()
    try:
        await anext(stream)
        await asyncio.sleep(0)
        assert not admission.done()
        assert owner._counts["execution"] == 1
        gate.set()
        await blocker
        second = await asyncio.wait_for(admission, 1)
        await second.wait()
        await owner.finish()
        assert owner.producer_count == 0
    finally:
        await owner.cancel()
        await asyncio.gather(admission, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_normal_finish_drains_execution_instead_of_cancelling_it():
    owner = owner_type()(turns=1, producers=4)
    gate = asyncio.Event()
    completed = []
    async def execute():
        await gate.wait()
        completed.append(True)
    task = owner.spawn(execute)
    finishing = asyncio.create_task(owner.finish())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()
        await finishing
        assert not task.cancelled()
        assert completed == [True]
        assert (await owner.completion).outcome == "completed"
    finally:
        await owner.cancel()


@pytest.mark.asyncio
async def test_dispatch_reserves_before_claim(tmp_path):
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.application.runtime.dispatcher import RuntimeDispatcher
    owner = owner_type()(turns=1, producers=4)
    held = owner.spawn(asyncio.Event().wait, role="renewal")
    store = ThreadStore(tmp_path / "atomic.db")
    dispatcher = RuntimeDispatcher(store=store, runner=None, lease_owner="worker", owner=owner)
    try:
        with pytest.raises(RuntimeError, match="capacity"):
            await dispatcher.dispatch_once()
        assert owner._counts["execution"] == 0
        assert not held.done()
    finally:
        await owner.cancel()

@pytest.mark.asyncio
async def test_dispatcher_runner_error_fails_run(tmp_path):
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.application.runtime.dispatcher import RuntimeDispatcher
    from voidx.agent.domain.profile import RuntimeProfile
    from voidx.agent.domain.thread import AgentThread, RuntimeDecision
    store = ThreadStore(tmp_path / "store.db")
    await store.create_thread(AgentThread(thread_id="loop"), profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"))
    loaded = await store.load("loop")
    attempt = await store.begin_attempt(thread_id="loop", source_outbox_id="seed", input_frame={},
                                        expected_state_version=loaded.state_version, lease_owner="seed", lease_seconds=60)
    await store.commit_decision(attempt_id=attempt.attempt_id, decision=RuntimeDecision(outcome="continue", summary="seed"),
                               expected_state_version=attempt.state_version, lease_owner="seed", fencing_token=attempt.fencing_token)
    owner = owner_type()(turns=1, producers=4)
    class Runner:
        async def run_turn(self, **kwargs):
            assert owner._counts["renewal"] == 1
            raise ValueError("private execution failure")
    dispatcher = RuntimeDispatcher(store=store, runner=Runner(), lease_owner="worker", owner=owner)
    propagated = []
    async def dispatch():
        with pytest.raises(ValueError, match="private execution failure"):
            await dispatcher.dispatch_once()
        propagated.append(True)
        raise RuntimeError("propagated")
    owner.spawn(dispatch, role="pump")
    with pytest.raises(RuntimeError, match="^Run failed$"):
        await asyncio.wait_for(anext(owner.events()), 2)
    assert propagated == [True]
    await owner.cancel()
    assert owner.producer_count == 0


@pytest.mark.asyncio
async def test_finish_allows_owned_descendants():
    owner = owner_type()(turns=1, producers=5)
    entered, proceed = asyncio.Event(), asyncio.Event()
    completed = []
    async def child():
        completed.append(True)
    async def execute(supervisor):
        entered.set()
        await proceed.wait()
        await supervisor.spawn_factory(child)
        return RunResult(completed=e.TurnCompletedPayload(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}))
    async def persist(result):
        pass
    async def cleanup(outcome):
        return ()
    await owner.register_turn(session_id="s", thread_id="t", execute=execute, persist=persist, cleanup=cleanup)
    await entered.wait()
    finishing = asyncio.create_task(owner.finish())
    await asyncio.sleep(0)
    proceed.set()
    await asyncio.wait_for(finishing, 2)
    assert completed == [True]
    assert (await owner.completion).outcome == "completed"

@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_real_pump_dispatcher_lease_failure(tmp_path, monkeypatch, raises):
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.domain.profile import RuntimeProfile
    from voidx.agent.domain.thread import AgentThread, RuntimeDecision
    store = ThreadStore(tmp_path / "store.db")
    await store.create_thread(AgentThread(thread_id="loop"), profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"))
    loaded = await store.load("loop")
    attempt = await store.begin_attempt(thread_id="loop", source_outbox_id="seed", input_frame={},
                                        expected_state_version=loaded.state_version, lease_owner="seed", lease_seconds=60)
    await store.commit_decision(attempt_id=attempt.attempt_id, decision=RuntimeDecision(outcome="continue", summary="seed"),
                               expected_state_version=attempt.state_version, lease_owner="seed", fencing_token=attempt.fencing_token)
    owner = owner_type()(turns=1, producers=4)
    from voidx.agent.application.runtime.pump import WakeupPumpMixin
    entered = asyncio.Event()
    stopped = asyncio.Event()
    async def lose_lease(*args, **kwargs):
        await entered.wait()
        if raises:
            raise OSError("private lease error")
        return False
    monkeypatch.setattr(store, "renew_attempt_lease", lose_lease)
    class Runner:
        async def run_turn(self, **kwargs):
            assert owner._counts["renewal"] == 1
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
    class Pump(WakeupPumpMixin):
        def _runner(self):
            return Runner()

        async def _owns_wakeup(self, thread_id):
            return thread_id == "loop"

    pump = Pump()
    pump._store = store
    pump._init_pump(lease_owner="worker", lease_seconds=.015, pump_poll_seconds=.001, owner=owner)
    pump.start_pump()
    with pytest.raises(RuntimeError, match="^Run failed$"):
        await asyncio.wait_for(anext(owner.events()), 2)
    assert entered.is_set() and stopped.is_set()
    assert pump._pump_task.done()
    await owner.cancel()
    assert owner.producer_count == 0


@pytest.mark.asyncio
async def test_finish_allows_reserved_renewal():
    owner = owner_type()(turns=1, producers=4)
    entered, proceed = asyncio.Event(), asyncio.Event()
    renewed = []
    async def renewal():
        renewed.append(True)
    async def dispatch():
        with owner.reserve_dispatch():
            entered.set()
            await proceed.wait()
            await owner.spawn(renewal, role="renewal")
    task = owner.spawn(dispatch, role="pump")
    await entered.wait()
    finishing = asyncio.create_task(owner.finish())
    await asyncio.sleep(0)
    proceed.set()
    await asyncio.wait_for(finishing, 2)
    assert renewed == [True]
    assert not task.cancelled()

@pytest.mark.asyncio
async def test_cancel_waits_direct_reserved_dispatch():
    owner = owner_type()(turns=1, producers=4)
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def dispatch():
        with owner.reserve_dispatch():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
    task = asyncio.create_task(dispatch())
    await entered.wait()
    try:
        await owner.cancel()
        assert stopped.is_set()
        assert task.done()
        assert owner._counts["execution"] == owner._counts["renewal"] == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _seed_real_wakeup_pump(tmp_path, owner, runner):
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.application.runtime.pump import WakeupPumpMixin
    from voidx.agent.domain.profile import RuntimeProfile
    from voidx.agent.domain.thread import AgentThread, RuntimeDecision

    store = ThreadStore(tmp_path / "pump.db")
    await store.create_thread(AgentThread(thread_id="loop"), profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"))
    loaded = await store.load("loop")
    attempt = await store.begin_attempt(thread_id="loop", source_outbox_id="seed", input_frame={},
                                        expected_state_version=loaded.state_version, lease_owner="seed", lease_seconds=60)
    await store.commit_decision(attempt_id=attempt.attempt_id, decision=RuntimeDecision(outcome="continue", summary="seed"),
                               expected_state_version=attempt.state_version, lease_owner="seed", fencing_token=attempt.fencing_token)

    class Pump(WakeupPumpMixin):
        def _runner(self):
            return runner

        async def _owns_wakeup(self, thread_id):
            return thread_id == "loop"

    pump = Pump()
    pump._store = store
    pump._init_pump(lease_owner="worker", lease_seconds=60, pump_poll_seconds=.001, owner=owner)
    return pump, store


@pytest.mark.asyncio
async def test_real_pump_reserves_before_claim_and_side_effect(tmp_path, monkeypatch):
    owner = owner_type()(turns=1, producers=4)
    held = owner.spawn(asyncio.Event().wait, role="renewal")
    pump, store = await _seed_real_wakeup_pump(tmp_path, owner, None)
    pending = await store.list_pending_outbox("loop")
    calls = []
    for name in ("claim_next_outbox", "begin_attempt", "mark_side_effect_started"):
        original = getattr(store, name)
        async def record(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return await _original(*args, **kwargs)
        monkeypatch.setattr(store, name, record)
    try:
        with pytest.raises(RuntimeError, match="capacity"):
            await pump._dispatch_next_wakeup()
        assert calls == []
        assert await store.list_pending_outbox("loop") == pending
        assert owner._counts["execution"] == 0
        assert not held.done()
    finally:
        await owner.cancel()


@pytest.mark.asyncio
async def test_real_pump_finish_waits_for_runner_without_cancelling(tmp_path):
    from voidx.agent.domain.thread import RuntimeDecision

    owner = owner_type()(turns=1, producers=4)
    entered, gate = asyncio.Event(), asyncio.Event()
    completed, cancelled = [], []

    class Runner:
        async def run_turn(self, **kwargs):
            entered.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            completed.append(True)
            return RuntimeDecision(outcome="completed", summary="done")

    pump, store = await _seed_real_wakeup_pump(tmp_path, owner, Runner())
    pump.start_pump()
    finishing = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        finishing = asyncio.create_task(owner.finish())
        done, _ = await asyncio.wait({finishing}, timeout=.05)
        assert not done
        assert cancelled == []
        assert completed == []
        gate.set()
        await asyncio.wait_for(finishing, 2)
        assert completed == [True]
        assert cancelled == []
        assert await store.list_pending_outbox("loop") == []
        assert pump._pump_task.exception() is None
        assert (await owner.completion).outcome == "completed"
        assert owner.producer_count == 0
        assert owner._counts["renewal"] == owner._counts["execution"] == 0
    finally:
        gate.set()
        await owner.cancel()
        if finishing is not None:
            await asyncio.gather(finishing, return_exceptions=True)
