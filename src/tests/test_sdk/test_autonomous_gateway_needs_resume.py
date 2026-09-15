"""Real evaluator commit interruption: durable needs_resume is not turn failure."""
import asyncio
import json
from collections import Counter

import pytest
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from tests.test_sdk.test_autonomous_gateway_projection import nodes
from tests.test_sdk.test_autonomous_session_recovery import seed_goal, sdk
from tests.test_sdk.test_autonomous_session_tools import identity
from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock, normalize_workspace
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.bootstrap.semantic_gateway import SemanticGatewayBridge
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events.consumers import DockEventConsumer


class NeedsResumeModel(FileRoundTripModel):
    _step: int = PrivateAttr(default=0)

    def _reply(self, messages):
        script = [
            ('goal_init', {'goal': 'Verify evidence', 'acceptance_condition': 'Evidence verified'}),
            ('goal_checkpoint', {'summary': 'Work evidence', 'progress': 'meaningful', 'evidence': ['verified']}),
            ('goal_decision', {'status': 'finished', 'summary': 'Verified', 'progress': 'meaningful', 'evidence': ['verified']}),
            (None, 'Evaluator verified evidence.'),
        ]
        assert self._step < len(script), 'needs_resume must not silently start another turn'
        name, value = script[self._step]
        self._step += 1
        return AIMessage(content=value) if name is None else AIMessage(content='', tool_calls=[{
            'id': f'needs-resume-{self._step}', 'name': name, 'args': value,
        }])


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered', [False, True], ids=['fresh-cancel', 'recovery-cancel'])
async def test_real_goal_needs_resume_gateway(tmp_path, monkeypatch, recovered):
    if recovered:
        root, store, _ = await seed_goal(tmp_path, monkeypatch)
    else:
        root = await create_session(workspace=str(tmp_path), profile='goal')
        store = ThreadStore()
    model = NeedsResumeModel()
    model._step = int(recovered)
    commit = ThreadStore.commit_goal_phase
    injected = []

    async def interrupt_evaluator_commit(self, **kwargs):
        record = await self.get_goal_protocol(kwargs['protocol_id'])
        if record.phase != 'decision':
            return await commit(self, **kwargs)
        assert not injected, 'fault must hit exactly one real evaluator commit'
        assert record.status == 'submitted'
        # The protocol exists, but its phase projection cannot commit. Use the
        # production fenced needs-resume transaction, not a synthetic event/state.
        loaded = await self.commit_goal_needs_resume(
            **{k: v for k, v in kwargs.items() if k != 'protocol_id'},
            phase='evaluator', reason='injected_evaluator_commit_interruption',
        )
        injected.append(loaded)
        return record

    monkeypatch.setattr(ThreadStore, 'commit_goal_phase', interrupt_evaluator_commit)
    dock = BottomInputDock()
    events, projected, notifications = [], [], []
    settled = asyncio.Event()

    class Consumer(DockEventConsumer):
        def handle(self, event):
            projected.append(event)
            return super().handle(event)

    agent = sdk(tmp_path, monkeypatch, model)
    bridge = SemanticGatewayBridge(agent, consumer=Consumer(dock), tree=dock.tree,
                                   workspace=str(tmp_path))

    class Client:
        async def send_text(self, text, **kwargs):
            message = json.loads(text)
            notifications.append(message)
            if message.get('method') == 'ui.request':
                p = message['params']
                assert await bridge.session._method_session_respond({
                    'request_id': p['request_id'], 'thread_id': p['thread_id'],
                    'value': 'approved',
                }) == {'ok': True}
            if (message.get('method') == 'turn.completed'
                    and message['params']['thread_id'].endswith(':evaluator')):
                settled.set()

    async with asyncio.timeout(35), bridge:
        await bridge.session.connect(Client())
        task = asyncio.create_task(bridge.run('Verify goal', session_id=root.id,
                                             workspace=str(tmp_path), observer=events.append))
        try:
            await settled.wait()
            assert len(injected) == 1
            binding, = await store.list_goal_generations(root.id)
            loaded = await store.load(binding.goal_thread_id)
            assert loaded.state.lifecycle.value == 'needs_user'
            assert loaded.state.context['goal_run']['phase_status'] == 'needs_resume'
            assert loaded.state.lifecycle_decision.outcome == 'needs_resume'
            assert loaded.state.context['goal_run']['interrupt_reason'] == 'injected_evaluator_commit_interruption'
            assert await store.list_pending_outbox(binding.goal_thread_id) == []
            # needs_user is an open autonomous stream, not successful completion.
            assert not task.done()
            with pytest.raises(ValueError, match='Cancellation thread'):
                await bridge.session._method_session_cancel({'thread_id': f'{binding.goal_thread_id}:evaluator'})
            assert await bridge.session._method_session_cancel({'thread_id': root.id}) == {'ok': True}
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    starts = [e for e in events if e.kind == 'turn.started']
    terminals = [e for e in events if e.kind in {'turn.completed', 'turn.failed', 'turn.cancelled'}]
    assert len(starts) == (2 if recovered else 3)
    assert Counter(identity(e) for e in terminals) == Counter(identity(e) for e in starts)
    assert all(count == 1 for count in Counter(identity(e) for e in terminals).values())
    assert {e.kind for e in terminals} == {'turn.completed'}
    children = [e for e in starts if e.session_id != root.id]
    assert {(e.session_id, e.thread_id) for e in children} == {
        (binding.work_session_id, binding.goal_thread_id),
        (binding.evaluator_session_id, f'{binding.goal_thread_id}:evaluator'),
    }
    statuses = [e for e in events if e.kind.startswith('status.')]
    assert [(e.kind, e.payload.stage) for e in statuses] == [
        ('status.updated', 'work'), ('status.updated', 'evaluator')]
    # Scheduler status describes the executed phase, not the store's lifecycle.
    # A successful child turn must not fabricate a finished goal status.
    assert identity(statuses[-1]) == identity(children[-1])
    assert json.loads(statuses[-1].payload.description)['phase'] == 'evaluator'
    assert not any(e.kind == 'status.finished' for e in projected)
    assert [(e.kind, e.label) for e in projected if e.kind.startswith('status.')] == [
        ('status.updated', 'work'), ('status.updated', 'evaluator')]
    client_starts = [n['params'] for n in notifications if n.get('method') == 'turn.started']
    client_ends = [n['params'] for n in notifications if n.get('method') in {
        'turn.completed', 'turn.failed', 'turn.cancelled'}]
    assert Counter(p['thread_id'] for p in client_starts) == Counter(e.thread_id for e in starts)
    expected = Counter((p['thread_id'], p['turn_id']) for p in client_starts)
    assert all(count == 1 for count in expected.values())
    assert Counter((p['thread_id'], p['turn_id']) for p in client_ends) == expected
    turns = [n for n in nodes(dock.tree.root) if n.node_type == 'turn']
    assert len(turns) == len(starts)
    assert [turn.payload['transcript_turn_id'] for turn in turns] == [
        bridge._turn_bindings[identity(e)] for e in starts]
    assert all(bridge._session_bindings[e.thread_id] == e.session_id for e in starts)
    for turn, started in zip(turns, starts, strict=True):
        assert turn.payload['raw_text'] == ('Verify goal' if started.session_id == root.id else started.payload.text)
    assert model._step == 4
    assert bridge.interactions.pending_count == 0
    assert bridge._run_task is None and not bridge._active
    assert not agent._active
    for kind, key in [('workspace', normalize_workspace(str(tmp_path))),
                      *[('session', sid) for sid in {root.id, *(e.session_id for e in starts)}]]:
        lock = HeadlessFileLock(kind, key)
        try:
            await asyncio.wait_for(lock.acquire(), 1)
        finally:
            lock.release()
