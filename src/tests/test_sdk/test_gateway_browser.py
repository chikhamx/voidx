"""Opt-in real Chromium acceptance; install Playwright and its Chromium separately.

Only the provider model is deterministic. Runs are host-started because production
session.submit has not migrated to the SDK bridge. No browser/RPC/graph mocks.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlencode

import pytest

pw = pytest.importorskip(
    "playwright.async_api", reason="Optional browser acceptance requires Playwright and Chromium"
)

from test_headless_runtime import FileRoundTripModel
from voidx.bootstrap.semantic_gateway import SemanticGatewayBridge
from voidx.config import Config, PermissionMode, Settings
from voidx.llm.adapters import langchain_model_factory
from voidx.presentation.gateway.server import GatewayServer
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.dock.status import PERMISSION_REQUEST_STATUS_ID
from voidx.presentation.output.events.consumers import DockEventConsumer
from voidx.sdk import VoidxAgent


async def wait_until(predicate, *, timeout=20):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


@asynccontextmanager
async def vite_frontend(tmp_path):
    frontend = Path(__file__).resolve().parents[3] / "frontend"
    vite = frontend / "node_modules/vite/bin/vite.js"
    if not shutil.which("node") or not vite.exists():
        pytest.skip("Optional browser acceptance requires Node and frontend npm dependencies")
    log = tmp_path / "vite.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            ["node", str(vite), "--host", "127.0.0.1", "--port", "0"],
            cwd=frontend, stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(30):
                while True:
                    text = log.read_text()
                    match = re.search(r"http://127\.0\.0\.1:\d+/", text)
                    if match:
                        break
                    assert process.poll() is None, text
                    await asyncio.sleep(0.1)
            yield match.group(0)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            assert process.poll() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["approve", "deny", "refresh", "reconnect", "second-client"])
async def test_real_chromium_gateway(tmp_path, monkeypatch, scenario):
    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = Config(workspace=str(workspace), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(workspace))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(workspace))
    server = GatewayServer(bridge.session, host="127.0.0.1", port=0)
    events, errors = [], []
    run = None
    browser = None
    async with vite_frontend(tmp_path) as url, pw.async_playwright() as playwright, bridge:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip("Optional browser acceptance requires an installed Playwright Chromium")
        try:
            await server.start()
            browser = await playwright.chromium.launch()
            print(f"Chromium={browser.version}; scenario={scenario}; workspace={workspace}")
            context = await browser.new_context()
            context.on("weberror", lambda error: errors.append(str(error.error)))
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            app_url = url + "?" + urlencode({"ws": server.url})
            await page.goto(app_url)
            await wait_until(lambda: len(bridge.session.clients) == 1)
            run = asyncio.create_task(bridge.run("Write sdk-probe.txt", workspace=str(workspace),
                                                observer=events.append))
            dialog = page.locator("#request-dialog[open]")
            await pw.expect(dialog).to_have_count(1)
            assert not errors, errors
            target = workspace / "sdk-probe.txt"
            assert not target.exists() and not run.done()
            assert bridge.interactions.pending_count == 1
            assert all(not actor.state.pending_requests
                       for actor in bridge.session._run_manager._actors.values())
            if scenario == "refresh":
                await page.reload()
                await pw.expect(dialog).to_have_count(1)
                await wait_until(lambda: len(bridge.session.clients) == 1)
            if scenario in {"reconnect", "second-client"}:
                old_clients = set(bridge.session.clients)
                # Close the real transport, leaving the document and Vite server intact.
                for client in old_clients:
                    await client._websocket.close(code=1012, reason="browser acceptance disconnect")
                await pw.expect(dialog).to_have_count(0)
                if scenario == "reconnect":
                    await wait_until(lambda: bool(bridge.session.clients)
                                     and not old_clients.intersection(bridge.session.clients))
                    await pw.expect(dialog).to_have_count(1, timeout=15000)
                else:
                    second = await context.new_page()
                    await second.goto(app_url)
                    await pw.expect(second.locator("#request-dialog[open]")).to_have_count(1)
                    await second.locator(".request-choice").filter(
                        has=second.locator(".request-choice-label", has_text=re.compile(r"^(Yes|Allow once)$"))
                    ).click()
                    await asyncio.wait_for(asyncio.shield(run), 20)
                    await wait_until(lambda: len(bridge.session.clients) == 2)
                    await pw.expect(second.locator("#request-dialog[open]")).to_have_count(0)
            if scenario != "second-client":
                label = r"^(No|Deny)$" if scenario == "deny" else r"^(Yes|Allow once)$"
                await dialog.locator(".request-choice").filter(
                    has=page.locator(".request-choice-label", has_text=re.compile(label))
                ).click()
                await asyncio.wait_for(asyncio.shield(run), 20)
            await pw.expect(dialog).to_have_count(0)
            assert sum(e.kind == "interaction.required" for e in events) == 1
            resolved = [e for e in events if e.kind == "interaction.resolved"]
            assert len(resolved) == 1
            assert resolved[0].payload.resolution.decision == ("deny" if scenario == "deny" else "approved")
            assert sum(e.kind in {"turn.completed", "turn.cancelled", "turn.failed"} for e in events) == 1
            assert events[-1].kind == "turn.completed"
            assert target.exists() == (scenario != "deny")
            if scenario != "deny":
                assert target.read_text() == "headless\n"
            assert not dock.active, "Bridge must release capture after the real graph run"
            assert bridge.interactions.pending_count == 0
            assert dock.status_record(PERMISSION_REQUEST_STATUS_ID) is None
            assert all(not actor.state.pending_requests
                       for actor in bridge.session._run_manager._actors.values())
            await pw.expect(page.locator("#transcript")).to_contain_text("FILE_WRITTEN")
            # Reload only here to verify persisted history, never as a reconnect substitute.
            await page.reload()
            await pw.expect(page.locator("#transcript")).to_contain_text("FILE_WRITTEN")
            await pw.expect(dialog).to_have_count(0)
            assert not errors, errors
        except Exception:
            if browser is not None:
                for index, open_page in enumerate(context.pages):
                    print(f"PAGE {index}: {await open_page.locator('body').inner_text()}")
            print(f"EVENTS: {[e.kind for e in events]}; pending={bridge.interactions.pending_count}")
            raise
        finally:
            try:
                if browser is not None:
                    await browser.close()
            finally:
                try:
                    if run is not None and not run.done():
                        await bridge.agent.cancel()
                        await asyncio.wait_for(run, 20)
                finally:
                    await server.stop()
            assert not bridge.session.clients
            assert not errors, errors


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["option", "free_text", "cancel", "reconnect", "no", "deny", "reject", "rejected"])
async def test_real_chromium_clarify(tmp_path, monkeypatch, scenario):
    from test_gateway_integration import ClarifyRoundTripModel
    from langchain_core.messages import ToolMessage
    from voidx.agent.adapters.persistence.session_repository import load_messages

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    negative = scenario in {"no", "deny", "reject", "rejected"}
    model = ClarifyRoundTripModel(options=[scenario, "Production"] if negative else ["Staging", "Production"])
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(workspace), permission_mode=PermissionMode.SAFE, lsp_format_after_edit=False)
    settings = Settings(str(workspace))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=DockEventConsumer(dock), tree=dock.tree, workspace=str(workspace))
    events, errors = [], []
    browser = run = None
    async with vite_frontend(tmp_path) as frontend, pw.async_playwright() as playwright, bridge:
        server = GatewayServer(session=bridge.session)
        await server.start()
        try:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(frontend + "?" + urlencode({"ws": server.url}))
            await wait_until(lambda: bool(bridge.session.clients))
            run = asyncio.create_task(bridge.run("Clarify the environment", workspace=str(workspace), observer=events.append))
            dialog = page.locator("#request-dialog")
            await pw.expect(dialog).to_be_visible(timeout=20000)
            await pw.expect(dialog).to_contain_text("Which environment?")
            await pw.expect(dialog.locator(".request-choice")).to_have_count(2)
            await pw.expect(dialog.locator("textarea")).to_have_count(1)
            request_id = await dialog.get_attribute("data-request-id")
            if scenario == "reconnect":
                await page.reload()
                await pw.expect(dialog).to_be_visible(timeout=20000)
                assert await dialog.get_attribute("data-request-id") == request_id
                await pw.expect(page.locator("dialog[open]")).to_have_count(1)
            if scenario == "free_text":
                await dialog.locator("textarea").fill("custom environment")
                await dialog.get_by_role("button", name="Submit", exact=True).click()
                expected = "custom environment"
            elif scenario == "cancel":
                await dialog.locator(".request-choice-cancel").click()
                expected = "User skipped clarification"
            elif negative:
                await dialog.get_by_role("button", name=scenario, exact=True).click()
                expected = f'"answer": "{scenario}"'
            else:
                await dialog.get_by_role("button", name="Staging", exact=True).click()
                expected = "Staging"
            await asyncio.wait_for(run, 25)
            await pw.expect(dialog).not_to_be_visible()
            assert sum(e.kind == "interaction.required" for e in events) == 1
            resolved = [e for e in events if e.kind == "interaction.resolved"]
            assert len(resolved) == 1
            assert resolved[0].payload.resolution.resolution_reason == ("dismissed" if scenario == "cancel" else "answered")
            assert resolved[0].payload.resolution.decision == ("skipped" if scenario == "cancel" else "answered")
            if negative:
                assert resolved[0].payload.resolution.value == scenario
                assert not resolved[0].payload.resolution.free_text
            assert sum(e.kind in {"turn.completed", "turn.cancelled", "turn.failed"} for e in events) == 1
            assert events[-1].kind == "turn.completed"
            assert bridge.interactions.pending_count == 0
            assert any(isinstance(m, ToolMessage) and expected in str(m.content) for h in model._histories for m in h)
            rows = await load_messages(events[0].session_id)
            assert any(row.role == "tool" and expected in str(row.content) for row in rows)
            await pw.expect(page.locator("#transcript")).to_contain_text("Which environment?")
            await pw.expect(page.locator("#transcript")).to_contain_text("CLARIFY_DONE")
            if negative:
                await pw.expect(page.locator("#transcript")).to_contain_text(f"Answer: {scenario}")
            await page.reload()
            await pw.expect(page.locator("#transcript")).to_contain_text("CLARIFY_DONE")
            if negative:
                await pw.expect(page.locator("#transcript")).to_contain_text(f"Answer: {scenario}")
            await pw.expect(dialog).not_to_be_visible()
            assert not dock.active
        finally:
            if browser is not None:
                await browser.close()
            if run is not None and not run.done():
                await bridge.agent.cancel()
                await asyncio.wait_for(run, 20)
            await server.stop()
        assert not errors, errors
        assert not bridge.session.clients


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "approved", "needs_doc", "rejected", "free_text", "modified",
    "first_refresh", "second_refresh", "second_cancel",
])
async def test_real_chromium_checkpoint(tmp_path, monkeypatch, scenario):
    import json
    from langchain_core.messages import ToolMessage
    from test_gateway_integration import CheckpointRoundTripModel
    from voidx.agent.adapters.persistence.runtime_state_repository import load_runtime_state
    from voidx.agent.adapters.persistence.session_repository import load_messages

    class BrowserCheckpointModel(CheckpointRoundTripModel):
        def _reply(self, messages):
            reply = super()._reply(messages)
            for call in reply.tool_calls:
                if call["name"] == "checkpoint":
                    call["args"].update(affected_files=["checkpoint-probe.py"], risks=["Keep safe policy"])
            return reply

    model = BrowserCheckpointModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = Config(workspace=str(workspace), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(workspace))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    dock = BottomInputDock()
    displayed = []

    class RecordingConsumer(DockEventConsumer):
        def handle(self, event):
            displayed.append(event)
            return super().handle(event)

    bridge = SemanticGatewayBridge(VoidxAgent(config, settings=settings),
        consumer=RecordingConsumer(dock), tree=dock.tree, workspace=str(workspace))
    server = GatewayServer(bridge.session, host="127.0.0.1", port=0)
    events, errors = [], []
    run = browser = None
    two_stage = scenario in {"modified", "second_refresh", "second_cancel"}
    scope = "approved" if two_stage else "Only inspect; do not write checkpoint-probe.py"
    decision = ("rejected" if scenario in {"rejected", "second_cancel"} else
                "modified" if two_stage or scenario == "free_text" else
                "approved" if scenario == "first_refresh" else scenario)
    async with vite_frontend(tmp_path) as url, pw.async_playwright() as playwright, bridge:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip("Optional browser acceptance requires an installed Playwright Chromium")
        try:
            await server.start()
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            context.on("weberror", lambda error: errors.append(str(error.error)))
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            print(f"Chromium={browser.version}; checkpoint={scenario}; workspace={workspace}")
            await page.goto(url + "?" + urlencode({"ws": server.url}))
            await wait_until(lambda: len(bridge.session.clients) == 1)
            run = asyncio.create_task(bridge.run("Present checkpoint", workspace=str(workspace), observer=events.append))
            dialog = page.locator("#request-dialog[open]")
            cards = page.locator("#transcript .checkpoint-row")
            await pw.expect(dialog).to_have_count(1, timeout=20000)
            await pw.expect(cards).to_have_count(1)
            required = [e.payload.request for e in events if e.kind == "interaction.required"]
            original = required[0]
            coordinator = bridge.agent._interactions
            card = dock._checkpoint_nodes[original.checkpoint_id]
            card_id = await cards.get_attribute("data-item-id")
            assert card_id == card.id
            assert original.interaction_id == original.checkpoint_id
            assert await dialog.get_attribute("data-request-id") == original.interaction_id
            for text in ["Wire checkpoint", "Steps", "Test", "Implement", "Affected files",
                         "checkpoint-probe.py", "Risks", "Keep safe policy"]:
                await pw.expect(cards.locator(".checkpoint-row-body")).to_contain_text(text)
            assert not run.done() and bridge.interactions.pending_count == 1
            assert not [e for e in events if e.kind == "interaction.resolved"]

            async def reload_pending(request_id):
                await page.reload()
                await pw.expect(dialog).to_have_count(1, timeout=20000)
                assert await dialog.get_attribute("data-request-id") == request_id
                await pw.expect(cards).to_have_count(1)
                assert await cards.get_attribute("data-item-id") == card_id
                await wait_until(lambda: len(bridge.session.clients) == 1)
                assert not run.done() and bridge.interactions.pending_count == 1

            if scenario == "first_refresh":
                await reload_pending(original.interaction_id)
            if scenario == "free_text":
                await dialog.locator("textarea").fill(scope)
                await dialog.get_by_role("button", name="Submit", exact=True).click()
            else:
                label = "Modify scope" if two_stage else {
                    "approved": "Implement directly", "first_refresh": "Implement directly",
                    "needs_doc": "Document first", "rejected": "Reject",
                }[scenario]
                await dialog.locator(".request-choice").filter(
                    has=page.locator(".request-choice-label", has_text=re.compile(f"^{label}$"))
                ).click()
            if two_stage:
                await pw.expect(dialog).to_contain_text("Describe the modified scope:")
                required = [e.payload.request for e in events if e.kind == "interaction.required"]
                assert len(required) == 2
                second = required[1]
                assert second.checkpoint_stage == "scope"
                assert second.checkpoint_id == original.checkpoint_id
                assert second.interaction_id != original.interaction_id
                assert await dialog.get_attribute("data-request-id") == second.interaction_id
                interim = [e.payload.resolution for e in events if e.kind == "interaction.resolved"]
                assert len(interim) == 1 and interim[0].decision == "modified"
                assert card.payload["decision"] == "modified"
                assert len(card.children) == 1 and not run.done()
                if scenario == "second_refresh":
                    await reload_pending(second.interaction_id)
                    await pw.expect(dialog).to_contain_text("Describe the modified scope:")
                if scenario == "second_cancel":
                    await dialog.locator(".request-choice-cancel").click()
                else:
                    await dialog.locator("textarea").fill(scope)
                    await dialog.get_by_role("button", name="Submit", exact=True).click()
            await asyncio.wait_for(asyncio.shield(run), 30)
            await pw.expect(dialog).to_have_count(0)
            required = [e.payload.request for e in events if e.kind == "interaction.required"]
            resolved = [e for e in events if e.kind == "interaction.resolved"]
            assert len(required) == len(resolved) == (2 if two_stage else 1)
            assert all(r.purpose == "checkpoint" for r in required)
            assert [e.payload.interaction_id for e in resolved] == [r.interaction_id for r in required]
            assert [e.payload.resolution.decision for e in resolved] == (["modified", decision] if two_stage else [decision])
            assert [r.checkpoint_stage for r in required] == (["decision", "scope"] if two_stage else ["decision"])
            expected_reason = ("dismissed" if scenario == "second_cancel" else
                               "user_rejected" if scenario == "rejected" else "answered")
            assert resolved[-1].payload.resolution.resolution_reason == expected_reason
            shown = [e for e in displayed if e.kind == "checkpoint_prompt.shown"]
            submitted = [e for e in displayed if e.kind == "checkpoint_decision.submitted"]
            assert len(shown) == len(dock._checkpoint_nodes) == 1
            assert [e.checkpoint_id for e in submitted] == [original.checkpoint_id] * len(required)
            assert len(card.children) == len(required)
            for child, event in zip(card.children, submitted, strict=True):
                assert (event.response or event.label or event.decision) in child.header
            assert card.payload["decision"] == decision
            assert sum(e.kind in {"turn.completed", "turn.cancelled", "turn.failed"} for e in events) == 1
            assert events[-1].kind == "turn.completed"
            assert [e.sequence for e in events] == list(range(1, len(events) + 1))
            assert len({e.event_id for e in events}) == len(events)
            results = [json.loads(m.content) for h in model._histories for m in h
                       if isinstance(m, ToolMessage) and m.tool_call_id == "checkpoint-probe"]
            rows = await load_messages(events[0].session_id)
            persisted = [json.loads(row.content) for row in rows if row.role == "tool"]
            assert results and persisted
            assert all(r["decision"] == decision for r in results + persisted)
            state = (await load_runtime_state(events[0].session_id)).task_state
            if decision != "rejected":
                expected_scope = scope if decision == "modified" else "Wire checkpoint"
                assert state.current_goal.desc == expected_scope
            if decision in {"approved", "needs_doc"}:
                target = "tdd" if decision == "approved" else "design"
                assert state.workflow_route.join == target
                assert state.workflow_route.leave == ("verify" if target == "tdd" else "design")
                assert state.workflow_runs[target].status.value == "active"
                assert state.workflow_runs[target].scope == "Wire checkpoint"
            else:
                assert not state.workflow_runs and state.workflow_route is None
            if decision == "modified":
                assert all(r["modified_scope"] == scope for r in results + persisted)
                assert resolved[-1].payload.resolution.free_text
            assert not (workspace / "checkpoint-probe.py").exists()
            assert config.permission_mode == PermissionMode.SAFE
            assert not coordinator._pending and not coordinator._checkpoint_scopes
            assert bridge.interactions.pending_count == 0 and not dock.active
            assert bridge.agent._run is None and bridge.agent._interactions is None
            assert all(not actor.state.pending_requests for actor in bridge.session._run_manager._actors.values())
            await pw.expect(page.locator("#transcript")).to_contain_text("CHECKPOINT_DONE")
            await page.reload()
            await pw.expect(page.locator("#transcript")).to_contain_text("CHECKPOINT_DONE")
            await pw.expect(cards).to_have_count(1)
            assert await cards.get_attribute("data-item-id") == card_id
            await pw.expect(cards.locator(".checkpoint-row-body")).to_contain_text("Keep safe policy")
            await pw.expect(dialog).to_have_count(0)
            assert not errors, errors
        finally:
            if browser is not None:
                await browser.close()
            if run is not None and not run.done():
                await bridge.agent.cancel()
                await asyncio.wait_for(run, 20)
            await server.stop()
        assert not errors, errors
        assert not bridge.session.clients
