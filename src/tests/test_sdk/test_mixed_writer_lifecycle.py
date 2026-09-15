"""Production legacy autonomous writers versus SDK owners."""
import asyncio
import json
from contextlib import suppress

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from pydantic import PrivateAttr
from websockets.asyncio.client import connect

from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.bootstrap.agent import build_agent_app
from voidx.config import Config, PermissionMode, Settings
from voidx.llm.adapters import langchain_model_factory


class MixedWriterModel(FileRoundTripModel):
    _iterations: int = PrivateAttr(default=0)
    _legacy_written: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _release: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    async def _astream(self, messages, **kwargs):
        self._histories.append(list(messages))
        sdk = any(isinstance(m, HumanMessage) and 'SDK_MIXED_WRITE' in str(m.content)
                  for m in messages)
        name = 'sdk-mixed.txt' if sdk else 'legacy-mixed.txt'
        call_id = 'sdk-mixed' if sdk else 'legacy-mixed'
        if any(isinstance(m, ToolMessage) and m.tool_call_id == call_id for m in messages):
            if not sdk:
                self._iterations += 1
                if self._iterations == 1:
                    yield ChatGenerationChunk(message=AIMessageChunk(content='', tool_calls=[
                        dict(id='continue-loop', name='loop_commit', args=dict(
                            outcome='continue', summary='First write completed',
                            progress='meaningful', next_delay_seconds=1))]))
                    return
                if self._iterations == 2:
                    yield ChatGenerationChunk(message=AIMessageChunk(content='First iteration done.'))
                    return
                if not any(isinstance(m, ToolMessage) and m.tool_call_id == 'legacy-background' for m in messages):
                    yield ChatGenerationChunk(message=AIMessageChunk(content='', tool_calls=[
                        dict(id='legacy-background', name='bash', args=dict(
                            command='printf legacy-mixed.txt > legacy-mixed.txt'))]))
                    return
                self._legacy_written.set()
                await self._release.wait()
            reply = AIMessage(content='Written.')
        else:
            reply = AIMessage(content='', tool_calls=[dict(id=call_id, name='bash',
                args=dict(command=f"printf {name} > {name}"))])
        yield ChatGenerationChunk(message=AIMessageChunk(content=reply.content, tool_calls=reply.tool_calls))


@pytest.mark.asyncio
@pytest.mark.parametrize('identity_only', [True, False], ids=['identity', 'mutex'])
async def test_production_legacy_loop_keeps_writer_after_dispatch(tmp_path, monkeypatch, identity_only):
    from voidx.bootstrap.production_sdk_gateway import ProductionSdkGateway
    from voidx.presentation.terminal import run_loop

    model = MixedWriterModel()
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: model)
    urls, hosts, errors = [], [], []
    from voidx.presentation.gateway.session.core import GatewaySession
    original_submit = GatewaySession._method_session_submit
    async def observed_submit(self, params):
        try:
            return await original_submit(self, params)
        except Exception as error:
            import traceback
            errors.append(traceback.format_exc())
            raise
    monkeypatch.setattr(GatewaySession, '_method_session_submit', observed_submit)
    def observed_error(error, **kwargs):
        import traceback
        errors.append(''.join(traceback.format_exception(error)))
    monkeypatch.setattr('voidx.presentation.protocol.v2.methods.log_internal_error', observed_error)
    gateways = []
    original_init = ProductionSdkGateway.__init__
    def observed_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        gateways.append(self)
    monkeypatch.setattr(ProductionSdkGateway, "__init__", observed_init)
    original = ProductionSdkGateway.legacy_completed
    def completed(self):
        original(self)
        hosts.append(self)
    monkeypatch.setattr(ProductionSdkGateway, 'legacy_completed', completed)
    monkeypatch.setattr(run_loop, 'emit_web_gateway_bootstrap', urls.append)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test')
    from voidx.agent.adapters.persistence.session_repository import create_session
    parent = await create_session(workspace=str(tmp_path), profile='coding')
    app = build_agent_app(config, 'local-test', session=parent, settings=settings)
    task = asyncio.create_task(app.run(web=True, web_headless=True))
    try:
        async with asyncio.timeout(40):
            while not urls:
                if task.done():
                    await task
                await asyncio.sleep(.02)
            async with connect(urls[0]) as ws:
                serial = 0
                async def rpc(method, params):
                    nonlocal serial
                    serial += 1
                    await ws.send(json.dumps(dict(jsonrpc='2.0', id=serial, method=method, params=params)))
                    while True:
                        message = json.loads(await ws.recv())
                        if message.get('id') == serial:
                            return message
                sdk = (await rpc('session.create', {'profile': 'coding'}))['result']['thread_id']
                if identity_only:
                    submitted = await rpc('session.submit', {'thread_id': sdk, 'text': 'SDK_MIXED_WRITE'})
                    assert 'error' not in submitted, submitted
                    while not (tmp_path / 'sdk-mixed.txt').exists():
                        await asyncio.sleep(.02)
                    assert (tmp_path / 'sdk-mixed.txt').read_text() == 'sdk-mixed.txt'
                    while gateways[-1].tasks:
                        await asyncio.sleep(.02)
                root = (await rpc('session.create', {'profile': 'coding'}))['result']['thread_id']
                started = await rpc('session.submit', {'thread_id': root, 'text': '/loop keep writing evidence'})
                assert 'error' not in started, started
                try:
                    await asyncio.wait_for(model._legacy_written.wait(), 12)
                except TimeoutError:
                    from voidx.persistence.sqlite import fetch_all
                    rows = await fetch_all("SELECT * FROM agent_thread_state")
                    outbox = await fetch_all("SELECT * FROM runtime_outbox")
                    pytest.fail(f'Legacy child did not write: {[dict(row) for row in rows]!r}; outbox={[dict(row) for row in outbox]!r}; histories={model._histories!r}')
                while not hosts:
                    await asyncio.sleep(.02)
                assert hosts[-1].legacy_pending == 0
                assert (tmp_path / 'legacy-mixed.txt').exists(), repr(model._histories)
                assert (tmp_path / 'legacy-mixed.txt').read_text() == 'legacy-mixed.txt'
                manager = hosts[-1].session._run_manager
                assert manager.status(root) == 'idle', manager.runtime_snapshot()
                assert not any(key.startswith('loop_') for key in manager.active_thread_ids()), manager.runtime_snapshot()
                if identity_only:
                    from voidx.persistence.sqlite import fetch_all
                    from voidx.platform.session_ids import validate_session_storage_id
                    rows = await fetch_all('SELECT id, session_id FROM agent_threads')
                    children = [row for row in rows if row['id'].startswith('loop:')]
                    assert children
                    for child in children:
                        assert validate_session_storage_id(child['session_id']) == child['session_id']
                        switched_child = await rpc('session.switch', {'thread_id': child['id']})
                        assert 'error' not in switched_child, (switched_child, errors)
                switched = await rpc('session.switch', {'thread_id': sdk})
                assert 'error' not in switched, '\n'.join(errors)
                if not identity_only:
                    submitted = await rpc('session.submit', {'thread_id': sdk, 'text': 'SDK_MIXED_WRITE'})
                    if 'error' in submitted:
                        manager = hosts[-1].session._run_manager
                        pytest.fail(f'{submitted!r}; actors={[(key, actor.state) for key, actor in manager._actors.items()]!r}; errors={errors!r}')
                    await asyncio.sleep(1)
                    assert not (tmp_path / 'sdk-mixed.txt').exists(), 'SDK wrote while a real legacy child still owned execution'
                    execution = app._run_loop._sessions._host
                    assert await execution.loop_service.stop(parent.id)
                    while gateways[-1].tasks:
                        await asyncio.sleep(.02)
                    assert (tmp_path / 'sdk-mixed.txt').read_text() == 'sdk-mixed.txt'
    finally:
        model._release.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_production_loop_child_session_identity(tmp_path):
    from voidx.agent.domain.automation.loop import LoopSpec
    from voidx.platform.session_ids import validate_session_storage_id

    config = Config(workspace=str(tmp_path))
    app = build_agent_app(config, None, settings=Settings(str(tmp_path)))
    # Inspect the actual composition, rather than replacing the runtime scheduler.
    execution = app._run_loop._sessions._host
    service = execution.loop_service
    identity = service._session_id_factory(LoopSpec(prompt='write evidence'), 'parent')
    assert validate_session_storage_id(identity) == identity
