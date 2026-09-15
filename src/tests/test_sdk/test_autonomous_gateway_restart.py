"""Independent process Gateway reader; receives thread IDs, never session bindings."""
import asyncio
import json
import os
from pathlib import Path

import websockets
import pytest

from langchain_core.messages import AIMessage
from tests.test_sdk.test_headless_runtime import FileRoundTripModel


class ContinuationModel(FileRoundTripModel):
    def _reply(self, messages):
        self._histories.append(list(messages))
        return AIMessage(content="CONTINUED")


async def read_restarted_gateway(data_dir, workspace, thread_ids):
    import voidx.persistence.sqlite as store
    from voidx.bootstrap.semantic_gateway import SemanticGatewayBridge
    from voidx.config import Config, Settings
    from voidx.sdk import VoidxAgent
    from voidx.presentation.output.dock import BottomInputDock
    from voidx.presentation.output.events.consumers import DockEventConsumer
    from voidx.presentation.gateway.server import GatewayServer
    from voidx.llm.adapters import langchain_model_factory

    store.DATA_DIR = Path(data_dir)
    def forbidden_model(*args, **kwargs):
        raise AssertionError('Restart restore must not run a model')
    langchain_model_factory.create_chat_model = forbidden_model
    langchain_model_factory.create_resolver_model = forbidden_model
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(Config(workspace=workspace), settings=Settings(workspace)),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=workspace)
    pages, snapshots = {}, {}
    async with bridge:
        server = GatewayServer(bridge.session, host='127.0.0.1', port=0)
        await server.start()
        try:
            async with websockets.connect(server.url) as socket:
                await asyncio.wait_for(socket.recv(), 30)
                for index, tid in enumerate(json.loads(thread_ids), 1):
                    await socket.send(json.dumps(dict(jsonrpc='2.0', id=index, method='transcript.page',
                        params={'thread_id': tid, 'turn_limit': 50})))
                    while True:
                        message = json.loads(await asyncio.wait_for(socket.recv(), 30))
                        if message.get('id') == index:
                            assert 'error' not in message, message
                            pages[tid] = message['result']
                            break
                    await socket.send(json.dumps(dict(jsonrpc='2.0', id=index + 1000,
                        method='session.switch', params={'thread_id': tid})))
                    while True:
                        message = json.loads(await asyncio.wait_for(socket.recv(), 30))
                        if message.get('method') == 'workspace.snapshot':
                            snapshots[tid] = message['params']['active_snapshot']
                        if message.get('id') == index + 1000:
                            assert 'error' not in message, message
                            break
                    assert snapshots[tid]['thread_id'] == tid
                    assert snapshots[tid]['nodes'] == pages[tid]['nodes']
                    assert snapshots[tid]['transcript_epoch'] == pages[tid]['transcript_epoch']
        finally:
            await server.stop()
    print(json.dumps({'pid': os.getpid(), 'pages': pages, 'snapshots': snapshots}))


async def continue_restarted_gateway(data_dir, workspace, session_id):
    import voidx.persistence.sqlite as store
    from voidx.bootstrap.semantic_gateway import SemanticGatewayBridge
    from voidx.config import Config, Settings
    from voidx.sdk import VoidxAgent
    from voidx.presentation.output.dock import BottomInputDock
    from voidx.presentation.output.events.consumers import DockEventConsumer
    from voidx.presentation.adapters.persistence.transcript_snapshot import load_transcript
    from voidx.llm.adapters import langchain_model_factory

    store.DATA_DIR = Path(data_dir)
    model = ContinuationModel()
    langchain_model_factory.create_chat_model = lambda *a, **kw: model
    langchain_model_factory.create_resolver_model = lambda *a, **kw: model
    config = Config(workspace=workspace)
    settings = Settings(workspace)
    settings.set_runtime_api_key(config.model.provider, 'local-test')
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=workspace)
    async with bridge:
        await bridge.run('RESTORE_PROBE', session_id=session_id, workspace=workspace)
    rows = await load_transcript(session_id)
    print(json.dumps({'pid': os.getpid(), 'rows': [r.model_dump() for r in rows],
                      'calls': len(model._histories)}))


@pytest.mark.asyncio
async def test_fresh_bridge_continuation_preserves_durable_turns(tmp_path):
    import sys
    import voidx.persistence.sqlite as store
    from voidx.agent.adapters.persistence.session_repository import create_session

    root = await create_session(workspace=str(tmp_path))
    history = []
    pids = []
    for index in range(2):
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c',
            'import asyncio, sys; from tests.test_sdk.test_autonomous_gateway_restart import '
            'continue_restarted_gateway; asyncio.run(continue_restarted_gateway(*sys.argv[1:]))',
            str(store.DATA_DIR), str(tmp_path), root.id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=os.environ.copy())
        stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        pids.append(result['pid'])
        rows = result['rows']
        assert result['calls'] == 1
        assert len({row['turn_id'] for row in rows}) == index + 1
        from voidx.persistence.jsonl import read_session_records
        records = await read_session_records(root.id, 'messages.jsonl') or []
        assert {record['type'] for record in records} <= {
            'message', 'message_replaced', 'message_deleted', 'session_cleared',
            'context_frame_deleted', 'runtime_state_deleted',
        }
        transcript = await read_session_records(root.id, 'transcript.jsonl') or []
        assert {record['type'] for record in transcript} <= {
            'turn_start', 'node', 'turn_end', 'transcript_reset',
        }
        identities = [record['metadata']['semantic_identity'] for record in transcript
                      if record['type'] == 'node']
        assert identities
        assert all(identity == {'session_id': root.id, 'thread_id': root.id}
                   for identity in identities)
        if history:
            assert [row for row in rows if row['turn_id'] == history[0]['turn_id']] == history
        else:
            history = rows
        tree_ids = [row['metadata']['tree_id'] for row in rows]
        assert len(tree_ids) == len(set(tree_ids))
    assert len(set(pids + [os.getpid()])) == 3


@pytest.mark.asyncio
async def test_metadata_discovery_ignores_reset_and_uncommitted_turns(tmp_path):
    from voidx.agent.adapters.persistence.session_repository import (
        create_session, semantic_thread_bindings,
    )
    from voidx.persistence.jsonl import append_session_records

    session = await create_session(workspace=str(tmp_path))
    node = {'type': 'node', 'turn_id': 1, 'metadata': {'semantic_identity': {
        'thread_id': 'opaque:event:thread', 'session_id': session.id}}}
    await append_session_records(session.id, 'transcript.jsonl', [
        {'type': 'turn_start', 'turn_id': 1}, node])
    assert await semantic_thread_bindings(str(tmp_path)) == {}
    await append_session_records(session.id, 'transcript.jsonl', [
        {'type': 'turn_end', 'turn_id': 1}])
    assert await semantic_thread_bindings(str(tmp_path)) == {'opaque:event:thread': session.id}
    await append_session_records(session.id, 'transcript.jsonl', [
        {'type': 'transcript_reset'}])
    assert await semantic_thread_bindings(str(tmp_path)) == {}
