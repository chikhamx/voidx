"""Scheduler output through real SDK child turns and durable dispatch."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from tests.test_sdk.test_autonomous_session_recovery import sdk
from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.tooling.domain.interaction import InteractionResponse


class StatusModel(FileRoundTripModel):
    _step: int = PrivateAttr(default=0)
    script: list = []

    def _reply(self, messages):
        if self._step >= len(self.script):
            return AIMessage(content='Done')
        name, args = self.script[self._step]
        self._step += 1
        if name is None:
            return AIMessage(content=args)
        return AIMessage(content='', tool_calls=[{'id': f'status-{self._step}', 'name': name, 'args': args}])


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['loop', 'goal'])
async def test_real_scheduler_status_is_committed_child_output(tmp_path, monkeypatch, profile):
    script = ([
        ('loop_init', {'goal': 'Two iterations'}),
        (None, 'Ready'),
        ('loop_start', {'goal': 'First'}),
        ('loop_commit', {'outcome': 'continue', 'summary': 'First done', 'progress': 'meaningful', 'next_delay_seconds': 1}),
        ('loop_start', {'goal': 'Second'}),
        ('loop_commit', {'outcome': 'continue', 'summary': 'Finished', 'progress': 'meaningful', 'next_delay_seconds': 60}),
    ] if profile == 'loop' else [
        ('goal_init', {'goal': 'Verify', 'acceptance_condition': 'Checked'}),
        ('goal_checkpoint', {'summary': 'Work', 'progress': 'meaningful', 'evidence': ['work']}),
        (None, 'Missing evaluator decision'),
    ])
    session = await create_session(workspace=str(tmp_path), profile=profile)
    events, observations = [], []
    async with sdk(tmp_path, monkeypatch, StatusModel(script=script)) as agent:
        async with asyncio.timeout(30):
            async for event in agent.stream('Start', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
                if event.kind == 'interaction.required':
                    request = event.payload.request
                    assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                        session_id=request.session_id, thread_id=request.thread_id,
                        turn_id=request.turn_id, value='approved'))
                if event.kind == 'turn.completed' and event.session_id != session.id:
                    children = [e for e in events if e.kind == 'turn.completed' and e.session_id != session.id]
                    if len(children) == 2:
                        await agent._run.cancel()
                if event.kind.startswith('status.') and event.payload.status_id.startswith(profile + ':'):
                    store = ThreadStore()
                    thread_id = await store.latest_thread_id_with_prefix(f'{profile}:{session.id}:')
                    loaded = await store.load(thread_id)
                    pending = await store.list_pending_outbox(thread_id)
                    observations.append((event, loaded, pending))
    assert observations, 'scheduler semantic status was swallowed'
    for event, loaded, pending in observations:
        assert event.session_id != session.id
        if profile == 'loop':
            assert event.thread_id == loaded.thread.thread_id
            assert event.session_id == loaded.thread.session_id
        else:
            binding = (await ThreadStore().list_goal_generations(session.id))[0]
            expected = binding.work_session_id if event.payload.stage == 'work' else binding.evaluator_session_id
            assert event.session_id == expected
            assert event.thread_id == loaded.thread.thread_id + (':evaluator' if event.payload.stage == 'evaluator' else '')
        turn = [e for e in events if e.turn_id == event.turn_id]
        assert turn[0].kind == 'turn.started'
        assert turn[-1].kind == 'turn.completed'
        assert sum(e.kind.startswith('turn.') and e.kind != 'turn.started' for e in turn) == 1
        assert [e.sequence for e in turn] == sorted({e.sequence for e in turn})
        if event.payload.stage == 'waiting':
            wakeups = [item for item in pending if item.kind == 'wakeup']
            assert len(wakeups) == 1
            assert json.loads(event.payload.description)['next_delay_seconds'] == wakeups[0].payload['decision']['next_delay_seconds']
            from voidx.persistence import sqlite
            import sqlite3
            with sqlite3.connect(sqlite.DATA_DIR / 'store' / 'voidx.db') as conn:
                available_at = conn.execute('SELECT available_at FROM runtime_outbox WHERE id = ?', (wakeups[0].outbox_id,)).fetchone()[0]
            assert json.loads(event.payload.description)['available_at'] == available_at
    if profile == 'loop':
        assert [e.payload.stage for e, _, _ in observations] == ['waiting', 'waiting']
        assert len({e.turn_id for e, _, _ in observations}) == 2
    else:
        assert [e.payload.stage for e, _, _ in observations] == ['work', 'evaluator']
        warning = [e for e in events if e.kind == 'diagnostic.warning' and e.payload.code == 'missing_goal_decision']
        assert len(warning) == 1
        assert warning[0].turn_id == observations[-1][0].turn_id


@pytest.mark.asyncio
async def test_scheduler_without_child_does_not_invent_turn():
    from voidx.agent.application.runtime.run_ownership import RunOwner
    from voidx.agent.application.runtime.scheduler_events import OwnedSchedulerEvents
    from voidx.agent.application.runtime.contracts import GoalPhaseResult

    owner = RunOwner()
    with pytest.raises(RuntimeError, match='missing_work_checkpoint'), owner.reserve_dispatch():
        await OwnedSchedulerEvents(owner).committed(goal_phase=GoalPhaseResult(
            phase='evaluator', attempt_number=1, needs_resume=True, reason='missing_work_checkpoint'))
    await owner.finish()
    assert [event async for event in owner.events()] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_full', [False, True])
async def test_needs_resume_on_owned_child_and_full_queue_cancel(cancel_full):
    from voidx.agent.application.runtime.contracts import GoalPhaseResult
    from voidx.agent.application.runtime.run_ownership import RunOwner
    from voidx.agent.application.runtime.run_supervisor import RunResult
    from voidx.agent.application.runtime.scheduler_events import OwnedSchedulerEvents
    from voidx.agent.domain import semantic_events as e

    owner = RunOwner(capacity=1)
    ready = asyncio.get_running_loop().create_future()
    publishing = asyncio.Event()
    async def dispatch():
        with owner.reserve_dispatch():
            async def execute(supervisor):
                from uuid import uuid4
                await supervisor.channel.publish(e.TurnStarted(
                    **supervisor.channel.identity, event_id=uuid4(), sequence=1, timestamp=0.0,
                    agent_id=None, parent_tool_call_id=None, payload={'text': 'input', 'metadata': {}}))
                return RunResult(completed=e.TurnCompletedPayload(usage={
                    'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}))
            async def persist(result):
                await owner.complete_after_dispatch(supervisor, ready)
            async def cleanup(outcome):
                return ()
            supervisor = await owner.register_turn(
                session_id='child', thread_id='goal:child:evaluator',
                execute=execute, persist=persist, cleanup=cleanup)
            await ready
            publishing.set()
            await OwnedSchedulerEvents(owner).committed(goal_phase=GoalPhaseResult(
                phase='evaluator', attempt_number=1, needs_resume=True, reason='missing_work_checkpoint'))
    task = owner.spawn(dispatch)
    await asyncio.wait_for(publishing.wait(), 2)
    if cancel_full:
        assert not task.done()
        await asyncio.wait_for(owner.cancel(), 2)
    else:
        task.add_done_callback(lambda _: owner.request_finish())
    async with asyncio.timeout(2):
        events = [event async for event in owner.events()]
    assert owner.producer_count == 0
    assert len({event.turn_id for event in events}) == 1
    assert events[-1].kind == ('turn.cancelled' if cancel_full else 'turn.completed')
    assert sum(event.kind in ('turn.completed', 'turn.cancelled', 'turn.failed') for event in events) == 1
    if not cancel_full:
        assert [event.kind for event in events] == [
            'turn.started', 'status.updated', 'diagnostic.warning', 'turn.completed']
        assert events[1].payload.stage == 'needs_resume'
        assert events[2].payload.code == 'goal_needs_resume'


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['loop', 'goal'])
async def test_real_status_time_and_terminal(tmp_path, monkeypatch, profile):
    from voidx.persistence import sqlite

    from voidx.agent.domain.automation.loop import LOOP_STALL_LIMIT

    script = ([('loop_init', {'goal': 'Wait for input', 'interval_seconds': 1}), (None, 'Ready')] + [
        call for iteration in range(LOOP_STALL_LIMIT - 1) for call in (
            ('loop_start', {'goal': f'Attempt {iteration + 1}'}),
            ('loop', {'operation': 'commit', 'outcome': 'continue',
                      'summary': 'No progress',
                      'progress': 'none', 'next_delay_seconds': 1}),
        )
    ] + [('loop_start', {'goal': 'Need user input'}),
         (None, ''), (None, 'Need input')] if profile == 'loop' else [
        ('goal_init', {'goal': 'Verify', 'acceptance_condition': 'Checked'}),
        ('goal_checkpoint', {'summary': 'Work', 'progress': 'meaningful', 'evidence': ['work']}),
        ('goal_decision', {'status': 'finished', 'summary': 'Checked', 'progress': 'meaningful', 'evidence': ['work']}),
        (None, 'Complete'),
    ])
    session = await create_session(workspace=str(tmp_path), profile=profile)
    events, statuses, wakeup_times = [], [], []
    async with sdk(tmp_path, monkeypatch, StatusModel(script=script)) as agent:
        async with asyncio.timeout(20):
            async for event in agent.stream('Start', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
                if event.kind == 'interaction.required':
                    request = event.payload.request
                    assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                        session_id=request.session_id, thread_id=request.thread_id,
                        turn_id=request.turn_id, value='approved'))
                if event.kind.startswith('status.') and event.payload.status_id.startswith(profile + ':'):
                    statuses.append(event)
                    if profile == 'loop' and event.kind == 'status.finished':
                        loaded = await ThreadStore().load(event.thread_id)
                        assert loaded.state.lifecycle.value == 'needs_user'
                    if event.payload.stage == 'waiting':
                        import sqlite3
                        with sqlite3.connect(sqlite.DATA_DIR / 'store' / 'voidx.db') as conn:
                            rows = conn.execute("SELECT available_at FROM runtime_outbox WHERE thread_id = ? AND kind = 'wakeup' AND delivered_at IS NULL", (event.thread_id,)).fetchall()
                        assert len(rows) == 1
                        wakeup_times.append(rows[0][0])
                if profile == 'loop' and event.kind == 'turn.completed' and statuses and statuses[-1].kind == 'status.finished':
                    await agent._run.cancel()
    assert len(statuses) == (LOOP_STALL_LIMIT if profile == 'loop' else 2)
    assert statuses[0].kind == 'status.updated'
    assert statuses[-1].kind == 'status.finished'
    assert statuses[-1].payload.ok == (profile == 'goal')
    for status in statuses:
        turn = [event for event in events if event.turn_id == status.turn_id]
        assert status.session_id != session.id
        assert turn[0].kind == 'turn.started'
        assert turn[-1].kind == 'turn.completed'
        assert turn.index(status) < len(turn) - 1
        assert [event.sequence for event in turn] == sorted({event.sequence for event in turn})
    if profile == 'loop':
        assert len(wakeup_times) == LOOP_STALL_LIMIT - 1
        for status, available_at in zip(statuses[:-1], wakeup_times, strict=True):
            assert status.kind == 'status.updated'
            assert json.loads(status.payload.description)['available_at'] == available_at
        assert json.loads(statuses[-1].payload.description)['reason'] == 'loop_stalled'
        assert json.loads(statuses[-1].payload.description)['outcome'] == 'needs_user'
        committed = [event for event in events if event.kind == 'assistant.committed'
                     and event.payload.text == 'Need input']
        assert len(committed) == 1
        terminal_text = committed[0]
        assert terminal_text.turn_id == statuses[-1].turn_id
        assert terminal_text.session_id == statuses[-1].session_id
        assert terminal_text.thread_id == statuses[-1].thread_id
        stream = [event for event in events
                  if getattr(event.payload, 'stream_id', None) == terminal_text.payload.stream_id]
        assert [event.kind for event in stream] == [
            'assistant.stream_started', 'assistant.chunk', 'assistant.committed']
        assert all(event.payload.phase == 'text' for event in stream)
        assert stream[1].payload.delta == 'Need input'
        assert events.index(terminal_text) < events.index(statuses[-1])
    else:
        binding = (await ThreadStore().list_goal_generations(session.id))[0]
        assert (await ThreadStore().load(binding.goal_thread_id)).state.lifecycle.value == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_real_pre_turn_needs_resume_has_no_child_and_settles(tmp_path, monkeypatch, cancel):
    from tests.test_sdk.test_autonomous_session_recovery import seed_goal, assert_root_released
    from voidx.persistence import sqlite

    session, store, binding = await seed_goal(tmp_path, monkeypatch)
    claim = ThreadStore.claim_next_outbox
    commit = ThreadStore.commit_goal_needs_resume
    committed = asyncio.Event()

    async def damaged_claim(self, **kwargs):
        # Corrupt the durable input after recovery, before the real dispatcher reads it.
        await sqlite.write_transaction(lambda conn: conn.execute(
            "UPDATE runtime_outbox SET payload_json = json_set(payload_json, '$.attempt_number', 999) WHERE thread_id = ? AND delivered_at IS NULL",
            (binding.goal_thread_id,)))
        return await claim(self, **kwargs)

    async def observed_commit(self, **kwargs):
        result = await commit(self, **kwargs)
        committed.set()
        if cancel:
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(ThreadStore, 'claim_next_outbox', damaged_claim)
    monkeypatch.setattr(ThreadStore, 'commit_goal_needs_resume', observed_commit)
    model = StatusModel()
    events = []
    async with sdk(tmp_path, monkeypatch, model) as agent:
        async def consume():
            async for event in agent.stream('Resume', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
        async with asyncio.timeout(10):
            task = asyncio.create_task(consume())
            if cancel:
                await committed.wait()
                await agent._run.cancel()
                await task
            else:
                with pytest.raises(RuntimeError, match='Run failed'):
                    await task
    assert committed.is_set()
    assert events == []
    assert model._step == 0
    assert (await store.load(binding.goal_thread_id)).state.lifecycle.value in ('failed', 'cancelled')
    await assert_root_released(session.id)
