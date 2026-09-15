"""Real SDK goal children restored over the Gateway wire without execution."""
import asyncio
import json

import pytest
import websockets

from tests.test_sdk.test_autonomous_gateway_projection import (
    ChildToolsModel, TwoLoopsModel, BottomInputDock, DockEventConsumer, SemanticGatewayBridge,
    Config, Settings, PermissionMode, VoidxAgent, langchain_model_factory, create_session,
)
from voidx.presentation.gateway.server import GatewayServer
from voidx.presentation.adapters.persistence.transcript_snapshot import load_transcript, transcript_epoch


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["goal", "loop"])
async def test_real_autonomous_child_durable_reconnect(tmp_path, monkeypatch, scenario):
    root = await create_session(workspace=str(tmp_path), profile=scenario)
    model = ChildToolsModel() if scenario == "goal" else TwoLoopsModel()
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test')
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(tmp_path))
    server = GatewayServer(bridge.session, host='127.0.0.1', port=0)
    events, snapshots = [], []
    request_id = 0

    async def receive(socket):
        message = json.loads(await asyncio.wait_for(socket.recv(), 30))
        if message.get('method') == 'workspace.snapshot':
            snapshots.append(message['params'])
        return message

    async def rpc(socket, method, params):
        nonlocal request_id
        request_id += 1
        await socket.send(json.dumps(dict(jsonrpc='2.0', id=request_id, method=method, params=params)))
        while True:
            message = await receive(socket)
            if message.get('id') == request_id:
                assert 'error' not in message, message
                return message['result']

    async with bridge:
        await server.start()
        run = None
        try:
            async with websockets.connect(server.url) as socket:
                await receive(socket)
                run = asyncio.create_task(bridge.run('Verify both file attempts', session_id=root.id,
                    workspace=str(tmp_path), observer=events.append))
                while not run.done():
                    incoming = asyncio.create_task(receive(socket))
                    done, _ = await asyncio.wait({incoming, run}, return_when=asyncio.FIRST_COMPLETED)
                    if incoming not in done:
                        incoming.cancel()
                        await asyncio.gather(incoming, return_exceptions=True)
                        break
                    message = incoming.result()
                    if (scenario == 'loop' and message.get('method') == 'turn.completed'
                            and sum(e.kind == 'turn.completed' and e.session_id != root.id for e in events) == 2):
                        await rpc(socket, 'session.cancel', {'thread_id': root.id})
                    if message.get('method') == 'ui.request':
                        p = message['params']
                        assert await rpc(socket, 'session.respond', dict(request_id=p['request_id'],
                            thread_id=p['thread_id'], value='approved' if p['thread_id'] == root.id else 'allow')) == {'ok': True}
                await run
            starts = [e for e in events if e.kind == 'turn.started']
            children = [e for e in starts if e.session_id != root.id]
            assert len(children) == (4 if scenario == "goal" else 2)
            assert len({e.session_id for e in children}) == (2 if scenario == "goal" else 1)
            calls = len(model._histories)
            assert model._step == (13 if scenario == "goal" else 6)
            bindings = {e.thread_id: e.session_id for e in starts}
            from voidx.persistence.jsonl import read_session_records
            for tid, sid in bindings.items():
                messages = await read_session_records(sid, 'messages.jsonl') or []
                assert {record['type'] for record in messages} <= {
                    'message', 'message_replaced', 'message_deleted', 'session_cleared',
                    'context_frame_deleted', 'runtime_state_deleted',
                }
                transcript = await read_session_records(sid, 'transcript.jsonl') or []
                assert {record['type'] for record in transcript} <= {
                    'turn_start', 'node', 'turn_end', 'transcript_reset',
                }
                nodes = [record for record in transcript if record['type'] == 'node']
                assert nodes
                assert all(record['metadata']['semantic_identity'] == {
                    'thread_id': tid, 'session_id': sid} for record in nodes)
            assert all(e.thread_id != e.session_id for e in children)
            # Reconnect must read disk, not the previous display tree or snapshot cache.
            dock.tree.root.children.clear()
            bridge.session._active_snapshot_cache = None
            async with websockets.connect(server.url) as socket:
                await receive(socket)
                pages, epochs = {}, {}
                for tid, sid in bindings.items():
                    page = await rpc(socket, 'transcript.page', {'thread_id': tid, 'turn_limit': 50})
                    assert page['nodes'], f'durable transcript missing for SDK session={sid}, thread={tid}'
                    assert page['thread_id'] == tid
                    assert page['transcript_epoch'] == await transcript_epoch(sid)
                    rows = await load_transcript(sid)
                    assert rows and {row.session_id for row in rows} == {sid}
                    assert len({row.turn_id for row in rows}) == sum(e.session_id == sid for e in starts)
                    await rpc(socket, 'session.switch', {'thread_id': tid})
                    snapshot = snapshots[-1]['active_snapshot']
                    assert snapshot['thread_id'] == tid
                    assert snapshot['nodes'] == page['nodes']
                    assert snapshot['transcript_epoch'] == page['transcript_epoch']
                    pages[tid] = json.dumps(page['nodes'])
                    epochs[tid] = page['transcript_epoch']
                first, second = (('EVALUATED_FIRST', 'EVALUATED_SECOND') if scenario == 'goal'
                                 else ('First done', 'Two done'))
                assert sum(first in text for text in pages.values()) == 1
                assert sum(second in text for text in pages.values()) == 1
                assert first not in pages[root.id] and second not in pages[root.id]
                assert len(set(pages.values())) == len(bindings)
            assert len(model._histories) == calls
            assert model._step == (13 if scenario == "goal" else 6)
        finally:
            if run is not None and not run.done():
                await bridge.agent.cancel()
                await run
            await server.stop()

    if scenario in {"goal", "loop"}:
        import os
        import sys
        import voidx.persistence.sqlite as store

        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c',
            'import asyncio, sys; from tests.test_sdk.test_autonomous_gateway_restart import '
            'read_restarted_gateway; asyncio.run(read_restarted_gateway(*sys.argv[1:]))',
            str(store.DATA_DIR), str(tmp_path), json.dumps(list(bindings)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
        assert process.returncode == 0, stderr.decode()
        restored = json.loads(stdout)
        assert restored['pid'] != os.getpid()
        for tid in bindings:
            assert restored['pages'][tid]['nodes'], f'restarted Gateway lost durable mapping: {tid}'
            assert json.dumps(restored['pages'][tid]['nodes']) == pages[tid]

            assert restored['pages'][tid]['transcript_epoch'] == epochs[tid]
