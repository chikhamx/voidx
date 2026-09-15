"""Recovery rejects conflicting durable identities before owning any work."""
import asyncio
import sqlite3

import pytest

from tests.test_sdk.test_autonomous_session_recovery import RecoveryModel, assert_root_released, sdk
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.application.automation.goal.goal_service import GoalService
from voidx.agent.application.automation.loop.loop_service import LoopService
from voidx.agent.application.automation.loop.scheduler import LoopRuntimeScheduler
from voidx.agent.domain.automation.goal import GoalProtocolRecord, GoalSpec, GoalSpecSnapshot
from voidx.agent.domain.automation.loop import LoopSpec
from voidx.persistence import sqlite as database
from voidx.persistence.sqlite import execute_commit


def durable_dump():
    with sqlite3.connect(database.DATA_DIR / 'store' / 'voidx.db') as connection:
        return list(connection.iterdump())


async def seed(tmp_path, monkeypatch, profile):
    session = await create_session(workspace=str(tmp_path), profile=profile)
    store = ThreadStore()
    boundary = 'initialize_goal_generation' if profile == 'goal' else 'enqueue_outbox'
    original = getattr(store, boundary)

    async def crash(**kwargs):
        await original(**kwargs)
        raise RuntimeError('durable boundary')

    monkeypatch.setattr(store, boundary, crash)
    service = (GoalService(store=store, workspace=str(tmp_path), scheduler=None) if profile == 'goal'
               else LoopService(store=store, workspace=str(tmp_path), scheduler=LoopRuntimeScheduler(
                   store=store, runtime=None, workspace=str(tmp_path))))
    with pytest.raises(RuntimeError, match='durable boundary'):
        await service.start(session.id, GoalSpec(objective='Recover', acceptance_condition='Verified')
                            if profile == 'goal' else LoopSpec(prompt='Recover'))
    thread_id = await store.latest_thread_id_with_prefix(f'{profile}:{session.id}:')
    return session, store, await store.load(thread_id)


async def reject(tmp_path, monkeypatch, session):
    before = durable_dump()
    model = RecoveryModel()
    events = []
    async with sdk(tmp_path, monkeypatch, model) as agent:
        with pytest.raises(RuntimeError, match='Run failed'):
            async with asyncio.timeout(10):
                async for event in agent.stream('No replacement', session_id=session.id, workspace=str(tmp_path)):
                    events.append(event)
    await assert_root_released(session.id)
    assert model._step == 0
    assert events == []
    assert durable_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('profile,phase', [('goal', 'work'), ('goal', 'evaluator'), ('loop', 'work')])
@pytest.mark.parametrize('field', ['workspace', 'root_session', 'profile_snapshot'])
async def test_child_identity_conflict(tmp_path, monkeypatch, profile, phase, field):
    session, store, loaded = await seed(tmp_path, monkeypatch, profile)
    child = loaded.thread.session_id
    if profile == 'goal':
        binding = (await store.list_goal_generations(session.id))[0]
        child = getattr(binding, f'{phase}_session_id')
    if field == 'workspace':
        await execute_commit('UPDATE sessions SET workspace = ? WHERE id = ?', (str(tmp_path / 'elsewhere'), child))
    elif field == 'root_session':
        await execute_commit('INSERT INTO provisional_sessions VALUES (?, ?, ?, ?)',
                             (child, 'other-root', 'other-owner', '2026-01-01'))
    else:
        other = await create_session(workspace=str(tmp_path), profile='coding')
        columns = ('runtime_profile', 'runtime_profile_revision', 'runtime_profile_content_hash',
                   'runtime_profile_hash', 'runtime_profile_source', 'runtime_profile_snapshot')
        await execute_commit('UPDATE sessions SET ' + ', '.join(
            f'{column} = (SELECT {column} FROM sessions WHERE id = ?)' for column in columns
        ) + ' WHERE id = ?', (*([other.id] * len(columns)), child))
    await reject(tmp_path, monkeypatch, session)


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', ['orphan', 'active'])
async def test_generation_candidates_are_not_arbitrarily_selected(tmp_path, monkeypatch, conflict):
    session, store, loaded = await seed(tmp_path, monkeypatch, 'goal')
    spec = GoalSpec(objective='Other', acceptance_condition='Other', generation='other-generation')
    snapshot = GoalSpecSnapshot.from_spec(spec, parent_session_id=session.id, parent_thread_id=session.id,
        workspace=str(tmp_path), profile_snapshot=session.profile_snapshot.model_dump(mode='json'))
    record = GoalProtocolRecord.submitted(protocol_id='other-init', parent_session_id=session.id,
        generation=spec.generation, phase='init', attempt_number=0, turn_id='other-turn',
        session_id=session.id, payload=snapshot)
    await store.submit_goal_protocol(record)
    if conflict == 'active':
        other_id = spec.goal_thread_id(session.id)
        state = loaded.state.model_copy(deep=True, update={'thread_id': other_id})
        state.context['goal_spec']['generation'] = spec.generation
        await store.create_thread(loaded.thread.model_copy(update={'thread_id': other_id}),
            profile=loaded.profile, state=state)
        children = [await create_session(workspace=str(tmp_path), profile='goal') for _ in range(2)]
        await execute_commit('INSERT INTO goal_generations SELECT ?, main_session_id, ?, ?, '
            '?, visibility, created_at, terminal_at, archived_at FROM goal_generations LIMIT 1',
            (spec.generation, children[0].id, children[1].id, other_id))
    await reject(tmp_path, monkeypatch, session)


@pytest.mark.asyncio
async def test_child_pinned_snapshot_wins_over_runtime_profile(tmp_path, monkeypatch):
    session, store, loaded = await seed(tmp_path, monkeypatch, 'goal')
    binding = (await store.list_goal_generations(session.id))[0]
    get_session = ThreadStore.get_session

    async def legacy_runtime(self, session_id):
        result = await get_session(self, session_id)
        if session_id in (binding.work_session_id, binding.evaluator_session_id):
            result = result.model_copy(update={'runtime_profile': 'unavailable'})
        return result

    monkeypatch.setattr(ThreadStore, 'get_session', legacy_runtime)
    model = RecoveryModel()
    async with sdk(tmp_path, monkeypatch, model) as agent:
        async with asyncio.timeout(15):
            events = [event async for event in agent.stream('Recover', session_id=session.id, workspace=str(tmp_path))]
    assert (await store.load(binding.goal_thread_id)).state.lifecycle.value == 'completed'
    assert {event.session_id for event in events} == {binding.work_session_id, binding.evaluator_session_id}
    await assert_root_released(session.id)


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['completed', 'cancelled', 'failed'])
@pytest.mark.parametrize('profile', ['goal', 'loop'])
async def test_terminal_allows_fresh_intake_without_reviving(tmp_path, monkeypatch, terminal, profile):
    from langchain_core.messages import AIMessage
    from tests.test_sdk.test_headless_runtime import FileRoundTripModel
    from voidx.agent.domain.thread import LifecycleState

    session, store, loaded = await seed(tmp_path, monkeypatch, profile)
    await store.save_state(loaded.thread.thread_id,
        loaded.state.model_copy(update={'lifecycle': LifecycleState(terminal)}),
        expected_state_version=loaded.state_version)
    before = await store.load(loaded.thread.thread_id)

    class IntakeModel(FileRoundTripModel):
        def _reply(self, messages):
            self._histories.append(list(messages))
            return AIMessage(content='Ready for a new request.')

    model = IntakeModel()
    async with sdk(tmp_path, monkeypatch, model) as agent:
        async with asyncio.timeout(10):
            events = [event async for event in agent.stream('Fresh intake', session_id=session.id, workspace=str(tmp_path))]
    assert model._histories
    assert {event.session_id for event in events} == {session.id}
    assert any(event.kind == 'turn.completed' for event in events)
    after = await store.load(loaded.thread.thread_id)
    assert after.state == before.state
    assert after.state_version == before.state_version
    await assert_root_released(session.id)


@pytest.mark.asyncio
async def test_multiple_active_loops_are_rejected(tmp_path, monkeypatch):
    session, store, loaded = await seed(tmp_path, monkeypatch, 'loop')
    other_id = f'loop:{session.id}:other-generation'
    await store.create_thread(loaded.thread.model_copy(update={'thread_id': other_id}),
        profile=loaded.resolved_profile, state=loaded.state.model_copy(update={'thread_id': other_id}))
    await reject(tmp_path, monkeypatch, session)
