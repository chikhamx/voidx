"""Recovery crosses real durable boundaries and executes the production graph."""
import asyncio

import pytest
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from tests.test_sdk.test_headless_execution import make_execution
from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.domain.automation.goal import GoalSpec
from voidx.bootstrap.autonomous_headless import build_autonomous_session
from voidx.config import Config, PermissionMode, Settings
from voidx.llm.adapters import langchain_model_factory
from voidx.sdk import VoidxAgent


class RecoveryModel(FileRoundTripModel):
    _step: int = PrivateAttr(default=0)

    def _reply(self, messages):
        script = [
            ('goal_checkpoint', {'summary': 'Recovered work', 'progress': 'meaningful', 'evidence': ['verified']}),
            ('goal_decision', {'status': 'finished', 'summary': 'Verified', 'progress': 'meaningful', 'evidence': ['verified']}),
            (None, 'Recovered goal complete.'),
        ]
        if self._step >= len(script):
            return AIMessage(content='No recovery controller.')
        name, value = script[self._step]
        self._step += 1
        return AIMessage(content=value) if name is None else AIMessage(content='', tool_calls=[
            {'id': f'recovery-{self._step}', 'name': name, 'args': value}])


async def seed_goal(tmp_path, monkeypatch):
    session = await create_session(workspace=str(tmp_path), profile='goal')
    store = ThreadStore()
    execution, _ = make_execution(tmp_path, session=session)
    assembly = build_autonomous_session(execution, workspace=str(tmp_path), session_id=session.id, store=store)
    initialize = store.initialize_goal_generation

    async def crash(**kwargs):
        await initialize(**kwargs)
        raise RuntimeError('injected crash after durable INIT')

    monkeypatch.setattr(store, 'initialize_goal_generation', crash)
    try:
        with pytest.raises(RuntimeError, match='after durable INIT'):
            await assembly.goal_service.start(session.id, GoalSpec(objective='Recover', acceptance_condition='Verified'))
    finally:
        await execution.aclose()
    bindings = await store.list_goal_generations(session.id)
    assert len(bindings) == 1
    return session, store, bindings[0]


def sdk(tmp_path, monkeypatch, model):
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test-not-a-credential')
    return VoidxAgent(config, settings=settings)


@pytest.mark.asyncio
async def test_sdk_recovers_durable_init_without_intake_or_new_children(tmp_path, monkeypatch):
    session, store, binding = await seed_goal(tmp_path, monkeypatch)
    events = []
    async with sdk(tmp_path, monkeypatch, RecoveryModel()) as agent:
        async with asyncio.timeout(25):
            async for event in agent.stream('Do not restart intake', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
    loaded = await store.load(binding.goal_thread_id)
    assert loaded.state.lifecycle.value == 'completed'
    bindings = await store.list_goal_generations(session.id)
    assert len(bindings) == 1
    assert bindings[0].model_dump(exclude={'terminal_at'}) == binding.model_dump(exclude={'terminal_at'})
    records = await store.list_goal_protocols(binding.generation)
    assert sum(record.phase == 'init' for record in records) == 1
    assert not any(event.kind == 'interaction.required' for event in events)
    assert {event.session_id for event in events} == {binding.work_session_id, binding.evaluator_session_id}
    assert {event.thread_id for event in events} == {binding.goal_thread_id, f'{binding.goal_thread_id}:evaluator'}


@pytest.mark.asyncio
async def test_sdk_corruption_is_failed_not_intake(tmp_path, monkeypatch):
    session, store, binding = await seed_goal(tmp_path, monkeypatch)
    loaded = await store.load(binding.goal_thread_id)
    state = loaded.state.model_copy(deep=True)
    state.context['goal_run']['projected_sequence_number'] = 99
    await store.save_state(binding.goal_thread_id, state, expected_state_version=loaded.state_version)
    model = RecoveryModel()
    events = []
    agent = sdk(tmp_path, monkeypatch, model)
    try:
        with pytest.raises(RuntimeError, match='Run failed'):
            async with asyncio.timeout(10):
                async for event in agent.stream('Do not restart', session_id=session.id, workspace=str(tmp_path)):
                    events.append(event)
        assert agent._active is False
    finally:
        await agent.aclose()
    loaded = await store.load(binding.goal_thread_id)
    assert loaded.state.lifecycle.value == 'failed'
    assert model._step == 0
    assert events == []
    assert len(await store.list_goal_generations(session.id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup_failure', [None, 'stop', 'full_queue'])
async def test_sdk_loop_resume_preserves_original_child_session(tmp_path, monkeypatch, cleanup_failure):
    from voidx.agent.domain.automation.loop import LoopSpec

    session = await create_session(workspace=str(tmp_path), profile='loop')
    store = ThreadStore()
    execution, _ = make_execution(tmp_path, session=session)
    assembly = build_autonomous_session(execution, workspace=str(tmp_path), session_id=session.id, store=store)
    assembly.loop_service._session_id_factory = lambda spec, parent: 'original-loop-child'
    class LoopModel(FileRoundTripModel):
        _step: int = PrivateAttr(default=0)

        def _reply(self, messages):
            self._step += 1
            name, args = ('loop_start', {'goal': 'Recovered iteration'}) if self._step % 2 else (
                'loop_commit', {'outcome': 'continue', 'summary': 'Recovered', 'progress': 'meaningful', 'next_delay_seconds': 1})
            return AIMessage(content='', tool_calls=[{'id': f'loop-recover-{self._step}', 'name': name, 'args': args}])

    enqueue = store.enqueue_outbox

    async def crash(**kwargs):
        await enqueue(**kwargs)
        raise RuntimeError('injected crash after loop outbox')

    monkeypatch.setattr(store, 'enqueue_outbox', crash)
    try:
        with pytest.raises(RuntimeError, match='after loop outbox'):
            await assembly.loop_service.start(session.id, LoopSpec(prompt='Continue original loop'))
    finally:
        await execution.aclose()
    thread_id = await store.latest_thread_id_with_prefix(f'loop:{session.id}:')
    before = await store.load(thread_id)
    pending = await store.list_pending_outbox(thread_id)
    await store.ack_outbox(pending[0].outbox_id)
    await enqueue(thread_id=thread_id, kind='wakeup', payload=pending[0].payload,
                  expected_state_version=before.state_version)

    events = []
    from contextlib import nullcontext

    with pytest.raises(RuntimeError, match="^Run failed$") if cleanup_failure else nullcontext():
        async with sdk(tmp_path, monkeypatch, LoopModel()) as agent:
            async with asyncio.timeout(10):
                async for event in agent.stream('Resume only', session_id=session.id, workspace=str(tmp_path)):
                    events.append(event)
                    if event.kind == 'turn.completed':
                        assert (await store.load(thread_id)).state.context['iteration'] == 1
                        run = agent._run
                        if cleanup_failure:
                            original_stop = run.assembly.loop_service.stop
    
                            async def stop(parent):
                                await original_stop(parent)
                                assert not run.owner.completion.done()
                                raise OSError('injected loop durable stop failure')
    
                            monkeypatch.setattr(run.assembly.loop_service, 'stop', stop)
                            if cleanup_failure == 'full_queue':
                                run.owner.mux.capacity = 1
                                while run.owner.mux.queued < 1:
                                    await asyncio.sleep(0.01)
                            with pytest.raises(BaseExceptionGroup) as caught:
                                await agent.cancel(session_id=session.id)
                            assert len(caught.value.exceptions) == 1
                            assert isinstance(caught.value.exceptions[0], OSError)
                            assert str(caught.value.exceptions[0]) == 'injected loop durable stop failure'
                            assert (await run.owner.completion).outcome == 'failed'
                            assert run.owner.producer_count == 0
                            assert run._session_lock._handle is None
                            assert run._workspace_lock._closed
                            continue
                        await agent.cancel(session_id=session.id)
    after = await store.load(thread_id)
    assert after.thread.session_id == before.thread.session_id == 'original-loop-child'
    assert after.state.context['iteration'] == 1
    assert after.state.lifecycle.value == 'cancelled'
    assert {event.session_id for event in events} == {'original-loop-child'}
    assert not any(event.kind == 'interaction.required' for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['completed', 'blocked', 'cancelled', 'failed'])
async def test_sdk_terminal_generation_is_not_revived(tmp_path, monkeypatch, terminal):
    from voidx.agent.domain.thread import LifecycleState

    session, store, binding = await seed_goal(tmp_path, monkeypatch)
    loaded = await store.load(binding.goal_thread_id)
    state = loaded.state.model_copy(update={'lifecycle': LifecycleState(terminal)})
    await store.save_state(binding.goal_thread_id, state, expected_state_version=loaded.state_version)
    before = await store.load(binding.goal_thread_id)

    class IdleModel(FileRoundTripModel):
        def _reply(self, messages):
            return AIMessage(content='No new goal requested.')

    events = []
    async with sdk(tmp_path, monkeypatch, IdleModel()) as agent:
        async with asyncio.timeout(10):
            async for event in agent.stream('Status only', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
    after = await store.load(binding.goal_thread_id)
    assert after.state == before.state
    assert after.state_version == before.state_version
    assert len(await store.list_goal_generations(session.id)) == 1
    assert {event.session_id for event in events} == {session.id}
    assert any(event.kind == 'turn.completed' for event in events)


async def assert_root_released(session_id):
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock

    lock = HeadlessFileLock('session', session_id)
    try:
        async with asyncio.timeout(1):
            await lock.acquire()
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_sdk_lease_conflict_does_not_stop_foreign_generation(tmp_path, monkeypatch):
    session, store, binding = await seed_goal(tmp_path, monkeypatch)
    assert await store.acquire_goal_generation_lease(binding.generation, 'foreign-owner', lease_seconds=60)
    before = await store.load(binding.goal_thread_id)
    pending = await store.list_pending_outbox(binding.goal_thread_id)
    model = RecoveryModel()
    async with sdk(tmp_path, monkeypatch, model) as agent:
        with pytest.raises(RuntimeError, match='Run failed'):
            async with asyncio.timeout(10):
                async for _ in agent.stream('No takeover', session_id=session.id, workspace=str(tmp_path)):
                    pytest.fail('Recovery conflict must not execute a turn')
        assert agent._active is False
    await assert_root_released(session.id)
    after = await store.load(binding.goal_thread_id)
    assert after.state == before.state
    assert after.state_version == before.state_version
    assert await store.list_pending_outbox(binding.goal_thread_id) == pending
    assert (await store.get_goal_generation_lease(binding.generation))['lease_owner'] == 'foreign-owner'
    assert model._step == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['work', 'evaluator'])
async def test_sdk_missing_goal_child_fails_before_dispatch(tmp_path, monkeypatch, phase):
    from voidx.persistence import sqlite

    session, store, binding = await seed_goal(tmp_path, monkeypatch)
    child_id = getattr(binding, f'{phase}_session_id')
    # Simulate damaged storage outside normal FK-protected application writes.
    import sqlite3
    with sqlite3.connect(sqlite.DATA_DIR / 'store' / 'voidx.db') as conn:
        conn.execute('DELETE FROM sessions WHERE id = ?', (child_id,))
    before = await store.load(binding.goal_thread_id)
    model = RecoveryModel()
    events = []
    async with sdk(tmp_path, monkeypatch, model) as agent:
        with pytest.raises(RuntimeError, match='Run failed'):
            async with asyncio.timeout(10):
                async for event in agent.stream('No replacement', session_id=session.id, workspace=str(tmp_path)):
                    events.append(event)
    await assert_root_released(session.id)
    assert events == []
    assert model._step == 0
    assert await store.get_session(child_id) is None
    after = await store.load(binding.goal_thread_id)
    assert after.state == before.state


@pytest.mark.asyncio
async def test_sdk_missing_loop_child_is_not_recreated(tmp_path, monkeypatch):
    from voidx.agent.domain.automation.loop import LoopSpec
    from voidx.persistence import sqlite

    session = await create_session(workspace=str(tmp_path), profile='loop')
    store = ThreadStore()
    execution, _ = make_execution(tmp_path, session=session)
    assembly = build_autonomous_session(execution, workspace=str(tmp_path), session_id=session.id, store=store)
    enqueue = store.enqueue_outbox

    async def crash(**kwargs):
        await enqueue(**kwargs)
        raise RuntimeError('after loop outbox')

    monkeypatch.setattr(store, 'enqueue_outbox', crash)
    try:
        with pytest.raises(RuntimeError, match='after loop outbox'):
            await assembly.loop_service.start(session.id, LoopSpec(prompt='Original loop'))
    finally:
        await execution.aclose()
    thread_id = await store.latest_thread_id_with_prefix(f'loop:{session.id}:')
    before = await store.load(thread_id)
    child_id = before.thread.session_id
    await sqlite.write_transaction(lambda conn: conn.execute('DELETE FROM sessions WHERE id = ?', (child_id,)))
    model = RecoveryModel()
    async with sdk(tmp_path, monkeypatch, model) as agent:
        with pytest.raises(RuntimeError, match='Run failed'):
            async with asyncio.timeout(2):
                async for _ in agent.stream('No replacement', session_id=session.id, workspace=str(tmp_path)):
                    pytest.fail('Missing child must fail before dispatch')
    await assert_root_released(session.id)
    assert await store.get_session(child_id) is None
    assert (await store.load(thread_id)).state == before.state
    assert model._step == 0


@pytest.mark.asyncio
async def test_sdk_recovers_standalone_submitted_init(tmp_path, monkeypatch):
    from voidx.agent.domain.automation.goal import GoalProtocolRecord, GoalSpecSnapshot

    session = await create_session(workspace=str(tmp_path), profile='goal')
    store = ThreadStore()
    spec = GoalSpec(objective='Recover', acceptance_condition='Verified', generation='standalone-init')
    snapshot = GoalSpecSnapshot.from_spec(
        spec, parent_session_id=session.id, parent_thread_id=session.id,
        workspace=str(tmp_path), profile_snapshot=session.profile_snapshot.model_dump(mode='json'),
    )
    record = GoalProtocolRecord.submitted(
        protocol_id='standalone-init-record', parent_session_id=session.id,
        generation=spec.generation, phase='init', attempt_number=0,
        turn_id='standalone-init-turn', session_id=session.id, payload=snapshot,
    )
    await store.submit_goal_protocol(record)
    assert await store.list_goal_generations(session.id) == []
    assert await store.latest_thread_id_with_prefix(f'goal:{session.id}:') is None
    events = []
    async with sdk(tmp_path, monkeypatch, RecoveryModel()) as agent:
        async with asyncio.timeout(15):
            async for event in agent.stream('Do not restart intake', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
    binding = await store.get_goal_generation(spec.generation)
    assert binding is not None
    assert (await store.load(binding.goal_thread_id)).state.lifecycle.value == 'completed'
    assert len(await store.list_goal_generations(session.id)) == 1
    records = await store.list_goal_protocols(spec.generation)
    assert records[0].protocol_id == record.protocol_id
    assert records[0].status == 'projected'
    assert sum(item.phase == 'init' for item in records) == 1
    assert {event.session_id for event in events} == {binding.work_session_id, binding.evaluator_session_id}
    assert not any(event.kind == 'interaction.required' for event in events)
    await assert_root_released(session.id)


@pytest.mark.asyncio
async def test_sdk_projects_submitted_checkpoint_before_evaluator_dispatch(tmp_path, monkeypatch):
    from tests.goal_protocol_helpers import submit_fenced_goal_protocol
    from voidx.agent.domain.automation.goal import GoalProtocolRecord, WorkCheckpoint

    session, store, binding = await seed_goal(tmp_path, monkeypatch)
    record = GoalProtocolRecord.submitted(
        protocol_id='durable-unprojected-checkpoint', parent_session_id=session.id,
        generation=binding.generation, phase='checkpoint', attempt_number=1,
        turn_id='crashed-work-turn', session_id=binding.work_session_id,
        payload=WorkCheckpoint(generation=binding.generation, attempt_number=1,
                               summary='Work already captured', work_turn_id='crashed-work-turn'),
    )
    source, attempt = await submit_fenced_goal_protocol(store, record)
    assert (await store.get_goal_protocol(record.protocol_id)).status == 'submitted'
    model = RecoveryModel()
    model._step = 1
    events = []
    async with sdk(tmp_path, monkeypatch, model) as agent:
        async with asyncio.timeout(15):
            async for event in agent.stream('Do not repeat work', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
    assert (await store.get_goal_protocol(record.protocol_id)).status == 'projected'
    assert (await store.get_attempt(attempt.attempt_id)).status == 'committed'
    assert all(item.outbox_id != source.outbox_id for item in await store.list_pending_outbox(binding.goal_thread_id))
    records = await store.list_goal_protocols(binding.generation)
    assert sum(item.phase == 'checkpoint' for item in records) == 1
    assert [item.sequence_number for item in records] == list(range(len(records)))
    assert (await store.load(binding.goal_thread_id)).state.lifecycle.value == 'completed'
    assert {event.session_id for event in events} == {binding.evaluator_session_id}
    assert not any(str(event.turn_id) == 'crashed-work-turn' for event in events)
    await assert_root_released(session.id)
