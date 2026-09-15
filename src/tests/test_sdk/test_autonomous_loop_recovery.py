"""Real SDK process death at loop persistence boundaries; no fabricated wakeups."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from tests.test_sdk.test_autonomous_session_recovery import sdk
from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.tooling.domain.interaction import InteractionResponse


class LoopRecoveryModel(FileRoundTripModel):
    _step: int = PrivateAttr(default=0)
    _resume: bool = PrivateAttr(default=False)

    def _reply(self, messages):
        self._histories.append(list(messages))
        script = ([] if self._resume else [
            ('loop_init', {'goal': 'Recover original loop'}),
            (None, 'Approved loop ready.'),
        ]) + [
            ('loop_start', {'goal': 'Resumed iteration' if self._resume else 'First iteration'}),
            ('loop_commit', {'outcome': 'continue', 'summary': 'Resumed done' if self._resume else 'First done',
                             'progress': 'meaningful', 'next_delay_seconds': 60 if self._resume else 3}),
        ]
        assert self._step < len(script), 'Completed iteration was rerun'
        name, args = script[self._step]
        self._step += 1
        if name is None:
            return AIMessage(content=args)
        return AIMessage(content='', tool_calls=[{
            'id': f'{"resume" if self._resume else "initial"}-{self._step}', 'name': name, 'args': args,
        }])


async def _crash_process(workspace, session_id, boundary):
    import voidx.persistence.sqlite as sqlite
    from voidx.agent.application.automation.loop.scheduler import LoopRuntimeScheduler
    from voidx.config.settings import Settings
    from voidx.config.adapters.profile_store import MemoryModelProfileStore

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sqlite, 'DATA_DIR', workspace / '.voidx')
    monkeypatch.setattr('voidx.config.settings._settings_home', lambda: workspace)
    original_settings = Settings.__init__
    monkeypatch.setattr(Settings, '__init__', lambda self, *a, **kw: original_settings(
        self, *a, **{**kw, 'profile_store': MemoryModelProfileStore()}))
    original_scheduler = LoopRuntimeScheduler.__init__
    def short_lease(self, **kwargs):
        original_scheduler(self, **{**kwargs, 'lease_seconds': 0.5})
    monkeypatch.setattr(LoopRuntimeScheduler, '__init__', short_lease)
    enqueue, commit, ack = ThreadStore.enqueue_outbox, ThreadStore.commit_decision, ThreadStore.ack_outbox

    async def die(store):
        thread_id = await store.latest_thread_id_with_prefix(f'loop:{session_id}:')
        loaded = await store.load(thread_id)
        pending = await store.list_pending_outbox(thread_id)
        rows = [dict(await store._one('SELECT * FROM runtime_outbox WHERE id = ?', (item.outbox_id,)))
                for item in pending]
        (workspace / 'crash.json').write_text(json.dumps({
            'thread_id': thread_id, 'session_id': loaded.thread.session_id,
            'state_version': loaded.state_version, 'context': loaded.state.context,
            'outbox': rows,
        }))
        os._exit(73)

    async def enqueue_crash(self, **kwargs):
        result = await enqueue(self, **kwargs)
        if boundary == 'initial' and kwargs['kind'] == 'loop_prompt':
            await die(self)
        return result

    async def commit_crash(self, **kwargs):
        result = await commit(self, **kwargs)
        if boundary == 'committed' and kwargs['decision'].outcome == 'continue':
            await die(self)
        return result

    async def ack_crash(self, outbox_id):
        await ack(self, outbox_id)
        if boundary == 'acked':
            await die(self)

    monkeypatch.setattr(ThreadStore, 'enqueue_outbox', enqueue_crash)
    monkeypatch.setattr(ThreadStore, 'commit_decision', commit_crash)
    monkeypatch.setattr(ThreadStore, 'ack_outbox', ack_crash)
    async with sdk(workspace, monkeypatch, LoopRecoveryModel()) as agent:
        async with asyncio.timeout(20):
            async for event in agent.stream('Recover original loop', session_id=session_id, workspace=str(workspace)):
                if event.kind == 'interaction.required':
                    request = event.payload.request
                    assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                        session_id=request.session_id, thread_id=request.thread_id,
                        turn_id=request.turn_id, value='approved'))
    raise AssertionError('Persistence fault boundary not reached')


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['initial', 'committed', 'acked'])
async def test_sdk_loop_process_crash_recovers_original_outbox(tmp_path, monkeypatch, boundary):
    session = await create_session(workspace=str(tmp_path), profile='loop')
    command = [sys.executable, '-c',
               'import asyncio,sys; from pathlib import Path; '
               'from tests.test_sdk.test_autonomous_loop_recovery import _crash_process; '
               'asyncio.run(_crash_process(Path(sys.argv[1]),sys.argv[2],sys.argv[3]))',
               str(tmp_path), session.id, boundary]
    process = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=40)
    assert process.returncode == 73, process.stdout + process.stderr
    snapshot = json.loads((tmp_path / 'crash.json').read_text())
    store = ThreadStore()
    thread_id = snapshot['thread_id']
    before = await store.load(thread_id)
    iteration = 0 if boundary == 'initial' else 1
    assert before.state.context.get('iteration', 0) == iteration
    assert before.state.lifecycle.value not in ('cancelled', 'failed', 'completed')
    target = next(row for row in snapshot['outbox'] if row['kind'] == ('loop_prompt' if boundary == 'initial' else 'wakeup'))
    original = dict(await store._one('SELECT * FROM runtime_outbox WHERE id = ?', (target['id'],)))
    assert original == target
    if iteration:
        assert before.state.lifecycle_decision.summary == 'First done'
        assert json.loads(target['payload_json'])['decision']['summary'] == 'First done'
        assert len(snapshot['outbox']) == (2 if boundary == 'committed' else 1)

    model = LoopRecoveryModel()
    model._resume = True
    events = []
    original_commit = ThreadStore.commit_decision
    committed = asyncio.Event()
    async def record_commit(self, **kwargs):
        result = await original_commit(self, **kwargs)
        committed.set()
        return result
    monkeypatch.setattr(ThreadStore, 'commit_decision', record_commit)
    async with sdk(tmp_path, monkeypatch, model) as agent:
        async with asyncio.timeout(12):
            async for event in agent.stream('Do not restart intake', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
                if event.kind == 'turn.started':
                    assert time.time() >= target['available_at'], 'Recovered wakeup ran before original due'
                if event.kind == 'turn.completed':
                    await asyncio.wait_for(committed.wait(), 2)
                    await agent.cancel(session_id=session.id)
    after = await store.load(thread_id)
    assert model._step == 2
    assert after.thread.session_id == snapshot['session_id']
    assert after.state.context['loop_spec'] == snapshot['context']['loop_spec']
    assert after.state.context['iteration'] == iteration + 1
    assert after.state.lifecycle.value == 'cancelled'
    assert after.state.lifecycle_decision.outcome == 'stop'
    assert await store.list_pending_outbox(thread_id) == []
    assert await store.latest_thread_id_with_prefix(f'loop:{session.id}:') == thread_id
    assert {event.session_id for event in events} == {snapshot['session_id']}
    assert {event.thread_id for event in events} == {thread_id}
    assert sum(event.kind == 'turn.started' for event in events) == 1
    assert not any(event.kind == 'interaction.required' for event in events)
    delivered = await store._one('SELECT * FROM runtime_outbox WHERE id = ?', (target['id'],))
    assert delivered['available_at'] == target['available_at']
    assert delivered['delivered_at'] is not None
    if iteration:
        assert any('First done' in str(message.content) for history in model._histories for message in history)
