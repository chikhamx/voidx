"""Production submission through real Vite/Chromium DOM; only LLM replies scripted.

No host-started bridge or browser RPC helpers. Refresh exercises in-process
reconnection, not a backend restart or the native Tauri shell.
"""
import asyncio
from collections import Counter
from contextlib import suppress
import json
from pathlib import Path
import re
from urllib.parse import urlencode

import pytest

from tests.test_sdk.test_gateway_browser import pw, vite_frontend, wait_until
from tests.test_sdk.test_autonomous_gateway_projection import TwoLoopsModel
from tests.test_sdk.test_autonomous_session_tools import ChildToolsModel, identity
from voidx.bootstrap.agent import build_agent_app
from voidx.config import Config, PermissionMode, Settings
from voidx.llm.adapters import langchain_model_factory
from voidx.sdk import VoidxAgent


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["goal", "loop"])
async def test_production_goal_loop_dom(tmp_path, monkeypatch, profile):
    from voidx.presentation.terminal import run_loop

    model = ChildToolsModel() if profile == "goal" else TwoLoopsModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    urls, calls, events, errors, wire = [], [], [], [], []
    stream = VoidxAgent.stream

    async def observed(self, prompt, **kwargs):
        calls.append((self, prompt, kwargs))
        async for event in stream(self, prompt, **kwargs):
            events.append(event)
            yield event

    monkeypatch.setattr(VoidxAgent, "stream", observed)
    monkeypatch.setattr(run_loop, "emit_web_gateway_bootstrap", urls.append)
    app = build_agent_app(config, "local-test", settings=settings)
    task = asyncio.create_task(app.run(web=True, web_headless=True))
    browser = None
    failure = None
    try:
        async with asyncio.timeout(110), vite_frontend(tmp_path) as url, pw.async_playwright() as playwright:
            assert Path(playwright.chromium.executable_path).exists(), "Required Chromium cache missing"
            await wait_until(lambda: bool(urls) or task.done())
            if task.done():
                await task
            session = app._run_loop._gateway_session
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            context.on("weberror", lambda error: errors.append(str(error.error)))
            page = await context.new_page()
            await page.goto(url + "?" + urlencode({"ws": urls[0]}))
            await wait_until(lambda: len(session.clients) == 1)
            client = next(iter(session.clients))
            send_text = client.send_text

            async def capture(text, **kwargs):
                wire.append(json.loads(text))
                await send_text(text, **kwargs)

            monkeypatch.setattr(client, "send_text", capture)
            await page.locator("#mode-trigger").click()
            await page.locator(f'#mode-menu [data-profile="{profile}"]').click()
            await pw.expect(page.locator(".vx-session-item.active")).to_have_count(1)
            root = await page.locator(".vx-session-item.active").get_attribute("data-thread-id")
            prompt = f"Production DOM {profile}: verify both attempts"
            await page.locator("#input").fill(prompt)
            await page.locator("#btn-send").click()
            await wait_until(lambda: bool(calls))
            assert calls[0][1] == prompt and calls[0][2]["session_id"] == root
            host = session._interaction_router
            handled = set()
            dialog = page.locator("#request-dialog[open]")
            cancelled = False
            while True:
                pending = [e for e in events if e.kind == "interaction.required"
                           and e.payload.request.interaction_id not in handled]
                if pending:
                    event = pending[0]
                    request = event.payload.request
                    try:
                        if await page.locator(".vx-session-item.active").get_attribute("data-thread-id") != event.thread_id:
                            await wait_until(lambda: any(m.get("method") == "ui.request"
                                and m["params"]["request_id"] == request.interaction_id for m in wire))
                            await page.wait_for_timeout(250)
                            child = page.locator(f'.vx-session-item[data-thread-id="{event.thread_id}"]')
                            mapping = await session._session_repository.semantic_thread_bindings(str(tmp_path))
                            evidence = {"expected_thread": event.thread_id, "expected_session": event.session_id,
                                "mapping": mapping, "rows": [r.model_dump(mode="json") for r in session.list_threads()],
                                "workspace_wire": [m for m in wire if m.get("method", "").startswith("workspace.")]}
                            assert await child.count() == 1, json.dumps(evidence, ensure_ascii=False)
                            await child.click()
                            await wait_until(lambda: session.active_thread_id == event.thread_id)
                        await pw.expect(dialog).to_have_count(1)
                    except (AssertionError, pw.TimeoutError):
                        print("BLOCKED_DOM_EVIDENCE=" + json.dumps({
                            "profile": profile, "model": type(model).__name__,
                            "model_step": model._step, "chromium": browser.version,
                            "root": root, "active_thread": session.active_thread_id,
                            "request": request.model_dump(mode="json"),
                            "approved_dom": len(handled), "sdk_submissions": len(calls),
                            "wire_requests": [m for m in wire if m.get("method") == "ui.request"],
                            "body": await page.locator("body").inner_text(),
                            "browser_errors": errors}, ensure_ascii=False))
                        raise
                    await wait_until(lambda: any(m.get("method") == "ui.request"
                        and m["params"]["request_id"] == request.interaction_id for m in wire))
                    projected = next(m["params"] for m in wire if m.get("method") == "ui.request"
                                     and m["params"]["request_id"] == request.interaction_id)
                    assert projected["thread_id"] == event.thread_id
                    assert identity(event) == (request.session_id, request.thread_id, request.turn_id)
                    if request.purpose == profile:
                        assert event.session_id == root
                        label = r"^Approve and start$"
                    else:
                        assert request.purpose == "permission" and event.session_id != root
                        label = r"^(Yes|Allow once)$"
                    await dialog.locator(".request-choice").filter(has=page.locator(
                        ".request-choice-label", has_text=re.compile(label))).click()
                    handled.add(request.interaction_id)
                    await wait_until(lambda: any(e.kind == "interaction.resolved"
                        and e.payload.interaction_id == request.interaction_id for e in events))
                elif profile == "loop" and sum(e.kind == "turn.completed"
                        and e.session_id != root for e in events) == 2:
                    assert session._run_manager.actor(root).is_active
                    await page.locator(f'.vx-session-item[data-thread-id="{root}"]').click()
                    await wait_until(lambda: session.active_thread_id == root)
                    await pw.expect(page.locator("#mode-stop")).to_be_visible()
                    await page.locator("#mode-stop").click()
                    await page.wait_for_timeout(300)
                    assert "Legacy commands require SDK runs" not in await page.locator("body").inner_text()
                    cancelled = True
                    await wait_until(lambda: not host.tasks)
                    break
                elif events and not host.tasks:
                    break
                else:
                    await asyncio.sleep(.05)
            starts = [e for e in events if e.kind == "turn.started"]
            terminals = [e for e in events if e.kind in {"turn.completed", "turn.cancelled", "turn.failed"}]
            assert len(starts) == (5 if profile == "goal" else 3)
            assert Counter(map(identity, starts)) == Counter(map(identity, terminals))
            assert not any(e.kind == "turn.failed" for e in events)
            assert len(handled) == (7 if profile == "goal" else 5)
            assert model._step == (13 if profile == "goal" else 6)
            if profile == "goal":
                assert (tmp_path / "child-evidence.txt").read_text() == "second evidence\n"
            else:
                assert cancelled
            assert not host.owners
            await pw.expect(dialog).to_have_count(0)
            bindings = {e.thread_id: e.session_id for e in starts}
            markers = (["First file written and read", "Second file written and read",
                        "EVALUATED_FIRST: first evidence; continue.",
                        "EVALUATED_SECOND: second evidence; finished."] if profile == "goal"
                       else ["First done", "Two done"])
            from voidx.presentation.adapters.persistence.transcript_snapshot import load_transcript
            durable = {tid: [row.model_dump(mode="json") for row in await load_transcript(sid)]
                       for tid, sid in bindings.items()}
            print("HISTORY_DURABLE_EVIDENCE=" + json.dumps(durable, ensure_ascii=False))
            for marker in markers:
                assert marker in json.dumps(durable, ensure_ascii=False), (marker, durable)
            async def collect_history():
                collected = {}
                for tid in bindings:
                    item = page.locator(f'.vx-session-item[data-thread-id="{tid}"]')
                    await item.click()
                    await pw.expect(item).to_have_class(re.compile(r"\bactive\b"))
                    await wait_until(lambda: session.active_thread_id == tid)
                    transcript = page.locator("#transcript")
                    snapshot = await session._active_thread_snapshot()
                    node_ids = [node.id for node in snapshot.nodes]
                    assert len(node_ids) == len(set(node_ids)), snapshot
                    turns = [node.id for node in snapshot.nodes if node.node_type == "turn"]
                    texts = []
                    async def visible_history_text():
                        collapsed = transcript.locator(".tool-group:has(> .tool-group-body[hidden]) > .tool-group-header")
                        while await collapsed.count():
                            await collapsed.first.click()
                        return await transcript.inner_text()
                    # History is virtualized: visit each turn using real browser scrolling.
                    for node_id in reversed(turns):
                        turn = transcript.locator(f'[data-item-id="{node_id}"]')
                        await transcript.hover()
                        try:
                            async with asyncio.timeout(10):
                                while await turn.count() == 0:
                                    texts.append(await visible_history_text())
                                    await page.mouse.wheel(0, -300)
                                    await asyncio.sleep(.05)
                        except TimeoutError:
                            print("HISTORY_SCROLL_BLOCKED=" + json.dumps({
                                "thread": tid, "turn": node_id,
                                "geometry": await transcript.evaluate("el => ({top: el.scrollTop, height: el.clientHeight, total: el.scrollHeight})"),
                                "html": await transcript.inner_html(),
                            }, ensure_ascii=False))
                            raise
                        await pw.expect(turn).to_have_count(1)
                        texts.append(await visible_history_text())
                    # A mounted turn header does not imply its assistant blocks are mounted.
                    # Traverse to the tail as well, collecting only actual rendered DOM text.
                    async with asyncio.timeout(10):
                        while True:
                            texts.append(await visible_history_text())
                            geometry = await transcript.evaluate(
                                "el => ({top: el.scrollTop, height: el.clientHeight, total: el.scrollHeight})"
                            )
                            if geometry["top"] + geometry["height"] >= geometry["total"] - 1:
                                break
                            await page.mouse.wheel(0, 300)
                            await asyncio.sleep(.05)
                    collected[tid] = "\n".join(texts)
                return collected

            histories = await collect_history()
            for marker in markers:
                assert any(marker in text for text in histories.values()), (marker, histories)
            saved_calls = (len(calls), model._step)
            old_clients = set(session.clients)
            await page.reload()
            await wait_until(lambda: bool(session.clients) and not old_clients.intersection(session.clients))
            reloaded = await collect_history()
            for tid in bindings:
                for marker in markers:
                    if marker in histories[tid]:
                        assert marker in reloaded[tid], (marker, tid, reloaded)
            assert saved_calls == (len(calls), model._step), "History navigation must not rerun the model"
            assert not errors, errors
            print("PRODUCTION_DOM_EVIDENCE=" + json.dumps({
                "profile": profile, "chromium": browser.version, "root": root,
                "bindings": bindings, "sdk_submissions": len(calls), "model": type(model).__name__,
                "model_steps": model._step, "approved_dom": len(handled), "cancel_dom": cancelled,
                "turns": len(starts), "histories": histories, "reloaded": reloaded,
                "browser_errors": errors}, ensure_ascii=False))
    except BaseException as error:
        failure = error
        print(f"PRODUCTION_DOM_FAILURE={error!r}; model_step={model._step}; events={[e.kind for e in events]}")
        raise
    finally:
        if browser is not None:
            await browser.close()
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        except Exception as cleanup_error:
            if failure is None:
                raise
            failure.add_note(f"Production shutdown also failed: {cleanup_error!r}")
