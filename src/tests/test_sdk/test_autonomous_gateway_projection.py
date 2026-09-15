"""Real autonomous LangGraph runs projected through Gateway client responses."""
import asyncio
import json
from collections import Counter

import pytest
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from tests.test_sdk.test_autonomous_session_tools import ChildToolsModel, identity
from tests.test_sdk.test_autonomous_session_recovery import seed_goal
from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.bootstrap.semantic_gateway import SemanticGatewayBridge
from voidx.config import Config, PermissionMode, Settings
from voidx.llm.adapters import langchain_model_factory
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events.consumers import DockEventConsumer
from voidx.sdk import VoidxAgent


class TwoLoopsModel(FileRoundTripModel):
    _step: int = PrivateAttr(default=0)

    def _reply(self, messages):
        script = [
            ('loop_init', {'goal': 'Two iterations'}),
            (None, 'Loop approved.'),
            ('loop_start', {'goal': 'First iteration'}),
            ('loop_commit', {'outcome': 'continue', 'summary': 'First done',
                             'progress': 'meaningful', 'next_delay_seconds': 1}),
            ('loop_start', {'goal': 'Second iteration'}),
            ('loop_commit', {'outcome': 'continue', 'summary': 'Two done',
                             'progress': 'meaningful', 'next_delay_seconds': 60}),
        ]
        assert self._step < len(script)
        name, value = script[self._step]
        self._step += 1
        return AIMessage(content=value) if name is None else AIMessage(content='', tool_calls=[
            {'id': f'projection-loop-{self._step}', 'name': name, 'args': value}])


class NeedsUserLoopModel(TwoLoopsModel):
    def _reply(self, messages):
        if self._step < 2:
            return super()._reply(messages)
        assert self._step < 12
        start = self._step % 2 == 0
        self._step += 1
        return AIMessage(content='', tool_calls=[{
            'id': f'needs-user-{self._step}',
            'name': 'loop_start' if start else 'loop_commit',
            'args': {'goal': 'Check for progress'} if start else {
                'outcome': 'continue', 'summary': 'No progress available',
                'progress': 'none', 'next_delay_seconds': 1,
            },
        }])


def nodes(node):
    yield node
    for child in node.children:
        yield from nodes(child)


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['goal', 'cancel', 'recovery_cancel', 'loop', 'loop_needs_user'])
async def test_real_autonomous_gateway_projection(tmp_path, monkeypatch, scenario):
    recovery = scenario == 'recovery_cancel'
    cancel = scenario in {'cancel', 'recovery_cancel'}
    if recovery:
        root, _, _ = await seed_goal(tmp_path, monkeypatch)
        model = ChildToolsModel()
        model._step = 1
    else:
        root = await create_session(workspace=str(tmp_path), profile='loop' if scenario.startswith('loop') else 'goal')
        model = (NeedsUserLoopModel() if scenario == 'loop_needs_user' else
                 TwoLoopsModel() if scenario == 'loop' else ChildToolsModel())
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test')
    dock = BottomInputDock()
    projected, events, notifications, requests = [], [], [], []

    class Consumer(DockEventConsumer):
        def handle(self, event):
            projected.append(event)
            return super().handle(event)

    agent = VoidxAgent(config, settings=settings)
    bridge = SemanticGatewayBridge(agent, consumer=Consumer(dock), tree=dock.tree,
                                   workspace=str(tmp_path))

    class Client:
        async def send_text(self, text, **kwargs):
            message = json.loads(text)
            notifications.append(message)
            if scenario.startswith('loop') and message.get('method') == 'turn.completed':
                child_done = [n for n in notifications if n.get('method') == 'turn.completed'
                              and n['params']['thread_id'] != root.id]
                if len(child_done) == (5 if scenario == 'loop_needs_user' else 2):
                    if scenario == 'loop_needs_user':
                        assert any(e.kind == 'status.finished' and e.payload.stage == 'idle'
                                   for e in events)
                    await bridge.session._method_session_cancel({'thread_id': root.id})
            if message.get('method') != 'ui.request':
                return
            p = message['params']
            requests.append(p)
            child = p['thread_id'] != root.id
            value = 'allow' if child else 'approved'
            if child:
                count = bridge.interactions.pending_count
                assert await bridge.session._method_session_respond({
                    'request_id': p['request_id'], 'thread_id': root.id, 'value': value,
                }) == {'ok': False}
                assert bridge.interactions.pending_count == count
                if cancel:
                    with pytest.raises(ValueError, match='Cancellation thread'):
                        await bridge.session._method_session_cancel({'thread_id': p['thread_id']})
                    assert await bridge.session._method_session_cancel({'thread_id': root.id}) == {'ok': True}
                    return
            assert await bridge.session._method_session_respond({
                'request_id': p['request_id'], 'thread_id': p['thread_id'], 'value': value,
            }) == {'ok': True}

    async with asyncio.timeout(45), bridge:
        await bridge.session.connect(Client())
        await bridge.run('ROOT PROMPT ONLY', session_id=root.id, workspace=str(tmp_path), observer=events.append)
    starts = [e for e in events if e.kind == 'turn.started']
    terminals = [e for e in events if e.kind in {'turn.completed', 'turn.failed', 'turn.cancelled'}]
    assert len(starts) == (6 if scenario == 'loop_needs_user' else
                          1 if recovery else 2 if cancel else 3 if scenario == 'loop' else 5)
    if scenario == 'loop_needs_user':
        statuses = [e for e in events if e.kind.startswith('status.') and e.payload.status_id == 'loop:waiting']
        assert [(e.kind, e.payload.stage) for e in statuses] == [
            *[('status.updated', 'waiting')] * 4, ('status.finished', 'idle')]
        assert json.loads(statuses[-1].payload.description)['outcome'] == 'needs_user'
        assert statuses[-1].payload.ok is False
        assert model._step == 12
        assert any(e.kind == 'status.finished' and e.label == 'idle' and not e.ok for e in projected)
    assert Counter(identity(e) for e in terminals) == Counter(identity(e) for e in starts)
    assert all(n == 1 for n in Counter(identity(e) for e in terminals).values())
    assert not any(e.kind == 'turn.failed' for e in events)
    assert bool([p for p in requests if p['thread_id'] == root.id]) is not recovery
    for event in projected:
        if event.kind == 'turn.started':
            assert event.raw_text == ('ROOT PROMPT ONLY' if event.thread_id == root.id else '')
    assert len([n for n in nodes(dock.tree.root) if n.node_type == 'turn']) == len(starts)
    assert bridge.interactions.pending_count == 0
    assert bridge._run_task is None and not bridge._active
    child_threads = {e.thread_id for e in starts if e.session_id != root.id}
    assert child_threads
    for thread_id in child_threads:
        assert await ThreadStore().list_pending_outbox(thread_id) == []
    assert notifications and requests
    if cancel:
        assert not (tmp_path / 'child-evidence.txt').exists()
        assert any(e.kind == 'turn.cancelled' for e in terminals)
    elif scenario == 'goal':
        assert (tmp_path / 'child-evidence.txt').read_text() == 'second evidence\n'
        assert {'tool_call', 'tool_result'} <= {n.node_type for n in nodes(dock.tree.root)}
    client_starts = [n['params'] for n in notifications if n.get('method') == 'turn.started']
    client_ends = [n['params'] for n in notifications
                   if n.get('method') in {'turn.completed', 'turn.failed', 'turn.cancelled'}]
    assert Counter(p['thread_id'] for p in client_starts) == Counter(e.thread_id for e in starts)
    assert Counter((p['thread_id'], p['turn_id']) for p in client_starts) == Counter(
        (p['thread_id'], p['turn_id']) for p in client_ends)
    assert len({(p['thread_id'], p['turn_id']) for p in client_starts}) == len(starts)
    turns = [n for n in nodes(dock.tree.root) if n.node_type == 'turn']
    for turn, started in zip(turns, starts, strict=True):
        assert turn.payload['raw_text'] == (
            'ROOT PROMPT ONLY' if started.session_id == root.id else started.payload.text)
    for request in requests:
        required = next(e for e in events if e.kind == 'interaction.required'
                        and e.payload.request.interaction_id == request['request_id'])
        assert request['thread_id'] == required.thread_id
        resolved = [e for e in events if e.kind == 'interaction.resolved'
                    and e.payload.interaction_id == request['request_id']]
        assert len(resolved) == 1 and identity(resolved[0]) == identity(required)
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock, normalize_workspace
    for kind, key in [('workspace', normalize_workspace(str(tmp_path))),
                      *[('session', sid) for sid in {root.id, *(e.session_id for e in starts)}]]:
        lock = HeadlessFileLock(kind, key)
        try:
            await asyncio.wait_for(lock.acquire(), 1)
        finally:
            lock.release()
