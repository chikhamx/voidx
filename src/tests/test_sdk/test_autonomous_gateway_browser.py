"""Real Vite UI, worker WebSocket and autonomous LangGraph acceptance.

Only provider replies are scripted. Runs are host-started; reconnect is in-process.
"""
import asyncio
from collections import Counter
import json
from pathlib import Path
import re
from urllib.parse import urlencode

import pytest
from langchain_core.messages import ToolMessage

from tests.test_sdk.test_gateway_browser import pw, vite_frontend, wait_until
from tests.test_sdk.test_autonomous_gateway_projection import (
    ChildToolsModel, TwoLoopsModel, BottomInputDock, DockEventConsumer,
    SemanticGatewayBridge, Config, Settings, PermissionMode, VoidxAgent,
    langchain_model_factory, create_session,
)
from tests.test_sdk.test_autonomous_session_tools import identity
from voidx.presentation.gateway.server import GatewayServer
from voidx.presentation.adapters.persistence.transcript_snapshot import load_transcript, transcript_epoch


async def client_rpc(page, method, params):
    return await page.evaluate("""async ([method, params]) => {
        const {rpcCall} = await import('/src/rpc/client.ts');
        return await rpcCall(method, params);
    }""", [method, params])


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['goal', 'loop'])
async def test_real_autonomous_gateway_browser(tmp_path, monkeypatch, scenario):
    root = await create_session(workspace=str(tmp_path), profile=scenario)
    model = ChildToolsModel() if scenario == 'goal' else TwoLoopsModel()
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
    events, errors, wire = [], [], []
    run = browser = None
    async with vite_frontend(tmp_path) as url, pw.async_playwright() as playwright, bridge:
        assert Path(playwright.chromium.executable_path).exists(), 'Required Chromium cache missing'
        try:
            await server.start()
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            context.on('weberror', lambda error: errors.append(str(error.error)))
            page = await context.new_page()
            # Worker-owned frames are captured at the server, without replacing the client.
            await page.goto(url + '?' + urlencode({'ws': server.url}))
            await wait_until(lambda: len(bridge.session.clients) == 1)
            client = next(iter(bridge.session.clients))
            original = client.send_text

            async def capture(text, **kwargs):
                wire.append(json.loads(text))
                await original(text, **kwargs)

            monkeypatch.setattr(client, 'send_text', capture)
            run = asyncio.create_task(bridge.run('Verify both file attempts', session_id=root.id,
                workspace=str(tmp_path), observer=events.append))
            dialog = page.locator('#request-dialog[open]')
            handled = set()
            cancelled = False
            async with asyncio.timeout(60):
                while not run.done():
                    pending = [e for e in events if e.kind == 'interaction.required'
                               and e.payload.request.interaction_id not in handled
                               and not any(r.kind == 'interaction.resolved' and
                                   r.payload.interaction_id == e.payload.request.interaction_id for r in events)]
                    if pending:
                        event = pending[0]
                        request = event.payload.request
                        await pw.expect(dialog).to_have_count(1)
                        await wait_until(lambda: any(m.get('method') == 'ui.request'
                            and m['params']['request_id'] == request.interaction_id for m in wire))
                        projected = next(m['params'] for m in wire if m.get('method') == 'ui.request'
                                         and m['params']['request_id'] == request.interaction_id)
                        assert identity(event) == (request.session_id, request.thread_id, request.turn_id)
                        assert projected['thread_id'] == event.thread_id
                        if request.purpose == scenario:
                            assert event.session_id == root.id
                            label = r'^(Approve and start)$'
                        else:
                            assert request.purpose == 'permission'
                            assert event.session_id != root.id and event.thread_id != root.id
                            assert await client_rpc(page, 'session.respond', {
                                'request_id': request.interaction_id, 'thread_id': root.id,
                                'value': 'allow'}) == {'ok': False}
                            assert bridge.interactions.pending_count == 1
                            label = r'^(Yes|Allow once)$'
                        await dialog.locator('.request-choice').filter(has=page.locator(
                            '.request-choice-label', has_text=re.compile(label))).click()
                        handled.add(request.interaction_id)
                        await wait_until(lambda: any(e.kind == 'interaction.resolved'
                            and e.payload.interaction_id == request.interaction_id for e in events))
                    elif scenario == 'loop' and sum(e.kind == 'turn.completed'
                            and e.session_id != root.id for e in events) == 2:
                        assert not run.done()
                        assert await client_rpc(page, 'session.cancel', {'thread_id': root.id}) == {'ok': True}
                        cancelled = True
                        await asyncio.wait_for(asyncio.shield(run), 20)
                    else:
                        await asyncio.sleep(.05)
            await run
            starts = [e for e in events if e.kind == 'turn.started']
            terminals = [e for e in events if e.kind in {'turn.completed', 'turn.cancelled', 'turn.failed'}]
            assert len(starts) == (5 if scenario == 'goal' else 3)
            assert Counter(map(identity, starts)) == Counter(map(identity, terminals))
            assert not any(e.kind == 'turn.failed' for e in events)
            for required in (e for e in events if e.kind == 'interaction.required'):
                resolved = [e for e in events if e.kind == 'interaction.resolved'
                            and e.payload.interaction_id == required.payload.request.interaction_id]
                assert len(resolved) == 1 and identity(resolved[0]) == identity(required)
                assert resolved[0].payload.resolution.decision == 'approved'
            assert len(handled) == (7 if scenario == 'goal' else 5)
            if scenario == 'goal':
                assert (tmp_path / 'child-evidence.txt').read_text() == 'second evidence\n'
                results = [e for e in events if e.kind == 'tool.result'
                           and e.payload.name in {'read', 'write'}]
                assert len(results) == 6
                assert all(identity(e) in set(map(identity, starts[1:])) for e in results)
                messages = {m.tool_call_id: str(m.content) for history in model._histories
                            for m in history if isinstance(m, ToolMessage)}
                for index, evidence in [(3, 'first evidence'), (5, 'first evidence'),
                                        (9, 'second evidence'), (11, 'second evidence')]:
                    assert evidence in messages[f'child-tools-{index}']
            else:
                assert cancelled and run.done()
            assert model._step == (13 if scenario == 'goal' else 6)
            calls = (model._step, len(model._histories))
            bindings = {e.thread_id: e.session_id for e in starts}
            assert len(bindings) == (3 if scenario == 'goal' else 2)
            old_clients = set(bridge.session.clients)
            dock.tree.root.children.clear()
            bridge.session._active_snapshot_cache = None
            for connection in old_clients:
                await connection._websocket.close(code=1012, reason='autonomous browser reconnect')
            await wait_until(lambda: bool(bridge.session.clients)
                             and not old_clients.intersection(bridge.session.clients))
            texts = {}
            for tid, sid in bindings.items():
                await client_rpc(page, 'session.switch', {'thread_id': tid, 'turn_limit': 50})
                snapshot = await client_rpc(page, 'transcript.page', {'thread_id': tid, 'turn_limit': 50})
                assert snapshot['thread_id'] == tid and snapshot['nodes']
                assert snapshot['transcript_epoch'] == await transcript_epoch(sid)
                rows = await load_transcript(sid)
                assert rows and {row.session_id for row in rows} == {sid}
                assert len({row.turn_id for row in rows}) == sum(e.session_id == sid for e in starts)
                texts[tid] = json.dumps(snapshot['nodes'])
                markers = (['First file written and read', 'Second file written and read',
                            'EVALUATED_FIRST: first evidence; continue.',
                            'EVALUATED_SECOND: second evidence; finished.'] if scenario == 'goal'
                           else ['First done', 'Two done'])
                for marker in markers:
                    if marker in texts[tid]:
                        await page.locator('#transcript').evaluate('(el) => { el.scrollTop = 0; el.dispatchEvent(new Event("scroll")); }')
                        await pw.expect(page.locator('#transcript')).to_contain_text(marker)
                    else:
                        await pw.expect(page.locator('#transcript')).not_to_contain_text(marker)
            for marker in markers:
                assert sum(marker in text for text in texts.values()) == 1
                assert marker not in texts[root.id]
            assert calls == (model._step, len(model._histories))
            assert bridge.interactions.pending_count == 0 and not errors, errors
            print(f'Chromium={browser.version}; scenario={scenario}; calls={calls}; threads={bindings}')
        except Exception:
            if browser:
                print(await page.locator('body').inner_text())
            raise
        finally:
            if run is not None and not run.done():
                await bridge.agent.cancel()
                await asyncio.wait_for(run, 20)
            if browser:
                await browser.close()
            await server.stop()
