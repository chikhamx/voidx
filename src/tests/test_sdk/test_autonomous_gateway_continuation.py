"""Real bridge processes share stores, not graph instances or semantic bindings."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
import websockets

from tests.test_sdk.test_autonomous_gateway_needs_resume import NeedsResumeModel
from tests.test_sdk.test_autonomous_loop_recovery import LoopRecoveryModel
from tests.test_sdk.test_autonomous_session_recovery import sdk


async def _process(workspace, root, scenario, phase):
    import voidx.persistence.sqlite as sqlite
    import voidx.bootstrap.semantic_gateway as gateway
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock, normalize_workspace
    from voidx.config.settings import Settings
    from voidx.config.adapters.profile_store import MemoryModelProfileStore
    from voidx.presentation.output.dock import BottomInputDock
    from voidx.presentation.output.events.consumers import DockEventConsumer
    from voidx.presentation.gateway.server import GatewayServer
    from voidx.agent.application.automation.loop.scheduler import LoopRuntimeScheduler
    from voidx.agent.application.automation.goal.scheduler import GoalRuntimeScheduler
    from voidx.presentation.adapters.persistence.transcript_snapshot import load_transcript

    workspace = Path(workspace)
    patch = pytest.MonkeyPatch()
    patch.setattr(sqlite, 'DATA_DIR', workspace / '.voidx')
    patch.setattr('voidx.config.settings._settings_home', lambda: workspace)
    original = Settings.__init__
    patch.setattr(Settings, '__init__', lambda self, *a, **kw: original(
        self, *a, **{**kw, 'profile_store': MemoryModelProfileStore()}))
    for cls in (LoopRuntimeScheduler, GoalRuntimeScheduler):
        init = cls.__init__
        def short(self, _init=init, **kwargs):
            _init(self, **{**kwargs, 'lease_seconds': 0.5})
        patch.setattr(cls, '__init__', short)
    model = LoopRecoveryModel() if scenario == 'loop' else NeedsResumeModel()
    if phase == 'second':
        if scenario == 'loop':
            model._resume = True
        else:
            model._step = 2 if scenario == 'goal' else 4
    store = ThreadStore()
    events, approvals = [], []
    dock = BottomInputDock()
    agent = sdk(workspace, patch, model)
    bridge = gateway.SemanticGatewayBridge(agent, consumer=DockEventConsumer(dock),
                                          tree=dock.tree, workspace=str(workspace))

    async def state():
        if scenario == 'loop':
            tid = await store.latest_thread_id_with_prefix(f'loop:{root}:')
            loaded = await store.load(tid)
            binding = {'thread_id': tid, 'session_id': loaded.thread.session_id}
            pairs = {root: root, tid: loaded.thread.session_id}
        else:
            binding, = await store.list_goal_generations(root)
            tid = binding.goal_thread_id
            pairs = {root: root, tid: binding.work_session_id,
                     f'{tid}:evaluator': binding.evaluator_session_id}
            binding = binding.model_dump(mode='json')
            loaded = await store.load(tid)
        return tid, pairs, binding, loaded

    resumed = asyncio.Event()
    if scenario == 'needs_resume' and phase == 'second':
        from voidx.agent.application.automation.goal.goal_service import GoalService
        resume = GoalService.resume_generation
        async def observe_resume(self, generation):
            result = await resume(self, generation)
            resumed.set()
            return result
        patch.setattr(GoalService, 'resume_generation', observe_resume)

    commit = ThreadStore.commit_goal_phase
    if scenario == 'needs_resume' and phase == 'first':
        async def interrupt(self, **kwargs):
            record = await self.get_goal_protocol(kwargs['protocol_id'])
            if record.phase != 'decision':
                return await commit(self, **kwargs)
            await self.commit_goal_needs_resume(
                **{k: v for k, v in kwargs.items() if k != 'protocol_id'},
                phase='evaluator', reason='process_test_commit_interruption')
            return record
        patch.setattr(ThreadStore, 'commit_goal_phase', interrupt)

    if scenario == 'goal' and phase == 'first':
        from voidx.agent.application.runtime.dispatcher import RuntimeDispatcher
        dispatch = RuntimeDispatcher._dispatch_claimed
        async def hold_evaluator(self, outbox):
            if outbox.payload.get('phase') == 'evaluator':
                await asyncio.Event().wait()
            return await dispatch(self, outbox)
        patch.setattr(RuntimeDispatcher, '_dispatch_claimed', hold_evaluator)

    append = gateway.append_transcript_turns
    async def persist(sid, turns):
        await append(sid, turns)
        if phase != 'first' or sid == root:
            return
        tid, pairs, binding, loaded = await state()
        if scenario == 'needs_resume' and sid != binding['evaluator_session_id']:
            return
        assert loaded.state.lifecycle.value not in {'completed', 'cancelled', 'failed'}
        if scenario == 'needs_resume':
            assert loaded.state.context['goal_run']['phase_status'] == 'needs_resume'
        rows = {key: [r.model_dump(mode='json') for r in await load_transcript(value)]
                for key, value in pairs.items()}
        (workspace / 'first.json').write_text(json.dumps({
            'pid': os.getpid(), 'binding': binding, 'pairs': pairs, 'rows': rows,
            'steps': model._step, 'approvals': approvals, 'events': events,
            'lifecycle': loaded.state.lifecycle.value}))
        os._exit(73)
    patch.setattr(gateway, 'append_transcript_turns', persist)

    async with asyncio.timeout(45), bridge:
        server = GatewayServer(bridge.session, host='127.0.0.1', port=0)
        await server.start()
        try:
            async with websockets.connect(server.url) as socket:
                await socket.recv()
                sequence = 0
                async def rpc(method, params):
                    nonlocal sequence
                    sequence += 1
                    await socket.send(json.dumps(dict(jsonrpc='2.0', id=sequence,
                                                      method=method, params=params)))
                    while True:
                        message = json.loads(await socket.recv())
                        if message.get('id') == sequence:
                            assert 'error' not in message, message
                            return message['result']
                before = {}
                if phase == 'second':
                    snapshot = json.loads((workspace / 'first.json').read_text())
                    for tid in snapshot['pairs']:
                        if snapshot['rows'][tid]:
                            before[tid] = await rpc('transcript.page', {'thread_id': tid, 'turn_limit': 50})
                task = asyncio.create_task(bridge.run(
                    'ROOT_PROMPT_MUST_NOT_BECOME_INTAKE' if phase == 'second' else 'Verify outcome',
                    session_id=root, workspace=str(workspace),
                    observer=lambda e: events.append(e.model_dump(mode='json'))))
                if phase == 'second' and scenario == 'needs_resume':
                    ready = asyncio.create_task(resumed.wait())
                    done, _ = await asyncio.wait({ready, task}, return_when=asyncio.FIRST_COMPLETED)
                    if task in done:
                        ready.cancel()
                        await asyncio.gather(ready, return_exceptions=True)
                        await task
                    await ready
                    await asyncio.sleep(1.1)
                    _, _, _, paused = await state()
                    assert paused.state.lifecycle.value == 'needs_user'
                    assert paused.state.context['goal_run']['phase_status'] == 'needs_resume'
                    assert not task.done()
                    assert model._step == 4 and events == []
                    await rpc('session.cancel', {'thread_id': root})
                else:
                    while not task.done():
                        receive = asyncio.create_task(socket.recv())
                        done, _ = await asyncio.wait({receive, task}, return_when=asyncio.FIRST_COMPLETED)
                        if receive not in done:
                            receive.cancel()
                            await asyncio.gather(receive, return_exceptions=True)
                            break
                        message = json.loads(receive.result())
                        if message.get('method') == 'ui.request':
                            assert phase == 'first', 'Recovery requested duplicate approval'
                            p = message['params']
                            approvals.append(p['request_id'])
                            await rpc('session.respond', {'request_id': p['request_id'],
                                'thread_id': p['thread_id'], 'value': 'approved'})
                        if (phase == 'second' and scenario == 'loop'
                                and message.get('method') == 'turn.completed'):
                            await rpc('session.cancel', {'thread_id': root})
                await task
                assert phase == 'second', 'First process missed crash boundary'
                tid, pairs, binding, loaded = await state()
                after = {key: await rpc('transcript.page', {'thread_id': key, 'turn_limit': 50})
                         for key in pairs}
                rows = {key: [r.model_dump(mode='json') for r in await load_transcript(sid)]
                        for key, sid in pairs.items()}
                assert loaded.state.lifecycle.value == ('completed' if scenario == 'goal' else 'cancelled')
                if scenario != 'loop':
                    protocols = await store.list_goal_protocols(binding['generation'])
                    assert [p.phase for p in protocols] == ['init', 'checkpoint', 'decision']
                    assert [p.status for p in protocols] == [
                        'projected', 'projected', 'submitted' if scenario == 'needs_resume' else 'projected']
                    assert [p.session_id for p in protocols] == [root, binding['work_session_id'],
                                                               binding['evaluator_session_id']]
                    assert await store.get_goal_generation_lease(binding['generation']) is None
                else:
                    assert loaded.state.context['iteration'] == 2
                assert await store.list_pending_outbox(tid) == []
                assert bridge.interactions.pending_count == 0 and not agent._active
                for kind, key in [('workspace', normalize_workspace(str(workspace))),
                                  *[('session', sid) for sid in set(pairs.values())]]:
                    lock = HeadlessFileLock(kind, key)
                    try:
                        await asyncio.wait_for(lock.acquire(), 1)
                    finally:
                        lock.release()
                (workspace / 'second.json').write_text(json.dumps({
                    'pid': os.getpid(), 'binding': binding, 'pairs': pairs, 'rows': rows,
                    'before': before, 'after': after, 'steps': model._step,
                    'events': events, 'approvals': approvals}))
        finally:
            await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['goal', 'loop', 'needs_resume'])
async def test_real_autonomous_process_continuation(tmp_path, scenario):
    from voidx.agent.adapters.persistence.session_repository import create_session
    root = await create_session(workspace=str(tmp_path), profile='loop' if scenario == 'loop' else 'goal')
    for phase, code in [('first', 73), ('second', 0)]:
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c', 'import asyncio,sys; '
            'from tests.test_sdk.test_autonomous_gateway_continuation import _process; '
            'asyncio.run(_process(*sys.argv[1:]))', str(tmp_path), root.id, scenario, phase,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=os.environ.copy())
        stdout, stderr = await asyncio.wait_for(process.communicate(), 65)
        assert process.returncode == code, stdout.decode() + stderr.decode()
        if phase == 'first':
            await asyncio.sleep(1)
    first, second = [json.loads((tmp_path / f'{p}.json').read_text()) for p in ('first', 'second')]
    assert len({os.getpid(), first['pid'], second['pid']}) == 3
    assert first['pairs'] == second['pairs']
    for key, value in first['binding'].items():
        if key != 'terminal_at':
            assert second['binding'][key] == value
    assert len(first['approvals']) == 1 and second['approvals'] == []
    assert second['steps'] == (2 if scenario == 'loop' else 4)
    assert all(e['session_id'] != root.id for e in second['events'] if e['kind'] == 'turn.started')
    for tid, old in first['rows'].items():
        new = second['rows'][tid]
        old_turns = {r['turn_id'] for r in old}
        assert [r for r in new if r['turn_id'] in old_turns] == old
        ids = [r['metadata']['tree_id'] for r in new]
        assert len(ids) == len(set(ids))
        if tid in second['before']:
            assert second['before'][tid]['transcript_epoch'] == second['after'][tid]['transcript_epoch']
            assert len(second['after'][tid]['nodes']) >= len(second['before'][tid]['nodes'])
        assert all(r['metadata']['semantic_identity'] == {
            'thread_id': tid, 'session_id': second['pairs'][tid]} for r in new)
        if scenario == 'needs_resume' or tid == root.id:
            assert new == old
    starts = [e for e in second['events'] if e['kind'] == 'turn.started']
    expected = [] if scenario == 'needs_resume' else [
        (second['binding']['session_id'], second['binding']['thread_id']) if scenario == 'loop' else
        (second['binding']['evaluator_session_id'], f"{second['binding']['goal_thread_id']}:evaluator")]
    assert [(e['session_id'], e['thread_id']) for e in starts] == expected
    terminals = [e for e in second['events'] if e['kind'] == 'turn.completed']
    assert [(e['session_id'], e['thread_id'], e['turn_id']) for e in terminals] == [
        (e['session_id'], e['thread_id'], e['turn_id']) for e in starts]
