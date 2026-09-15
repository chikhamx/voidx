"""Production build_agent_app / run loop / WebSocket SDK submission contract."""
import asyncio
import json
from contextlib import suppress

import pytest
from websockets.asyncio.client import connect

from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from tests.test_sdk.test_autonomous_session_tools import ChildToolsModel
from tests.test_sdk.test_autonomous_gateway_projection import TwoLoopsModel
from voidx.bootstrap.agent import build_agent_app
from voidx.config import Config, PermissionMode, Settings
from voidx.llm.adapters import langchain_model_factory
from voidx.sdk import VoidxAgent


@pytest.mark.asyncio
async def test_sdk_host_rejects_submission_while_legacy_command_is_queued(tmp_path):
    from voidx.bootstrap.production_sdk_gateway import ProductionSdkGateway
    from voidx.presentation.gateway.session.core import GatewaySession
    from voidx.presentation.output.tree import OutputTree
    from voidx.presentation.protocol import UiSubmitCommand
    from voidx.presentation.protocol.v2.methods import MethodParamsError

    queued = []
    async def legacy(command):
        queued.append(command)

    session = GatewaySession(OutputTree, workspace=str(tmp_path))
    host = ProductionSdkGateway(session, legacy, config=Config(workspace=str(tmp_path)), settings=Settings(str(tmp_path)))
    try:
        await host.handle(UiSubmitCommand(thread_id="legacy", text="/init", workspace=str(tmp_path)))
        assert len(queued) == 1
        with pytest.raises(MethodParamsError, match="Legacy"):
            await host.handle(UiSubmitCommand(thread_id="sdk", text="write a file", workspace=str(tmp_path)))
        from voidx.presentation.protocol import UiResponse
        pending = asyncio.get_running_loop().create_future()
        session._run_manager.actor("legacy").register_pending_request("legacy-approval", pending)
        assert not await host.respond("legacy-approval", "allow", thread_id="wrong")
        assert await host.respond("legacy-approval", "allow", thread_id="legacy")
        assert (await pending).value == "allow"
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_production_sdk_switch_queue_cancel_and_close(tmp_path, monkeypatch):
    from voidx.presentation.terminal import run_loop

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    urls, calls = [], []
    stream = VoidxAgent.stream

    async def observed(self, prompt, **kwargs):
        calls.append(self)
        async for event in stream(self, prompt, **kwargs):
            yield event

    monkeypatch.setattr(VoidxAgent, "stream", observed)
    monkeypatch.setattr(run_loop, "emit_web_gateway_bootstrap", urls.append)
    app = build_agent_app(config, "local-test", settings=settings)
    task = asyncio.create_task(app.run(web=True, web_headless=True))
    try:
        async with asyncio.timeout(35):
            while not urls:
                if task.done():
                    await task
                await asyncio.sleep(.02)
            async with connect(urls[0]) as ws:
                serial = 0
                messages = []

                async def rpc(method, params):
                    nonlocal serial
                    serial += 1
                    await ws.send(json.dumps(dict(jsonrpc="2.0", id=serial, method=method, params=params)))
                    while True:
                        message = json.loads(await ws.recv())
                        messages.append(message)
                        if message.get("id") == serial:
                            assert "error" not in message, message
                            return message["result"]

                first = (await rpc("session.create", {"profile": "coding"}))["thread_id"]
                await rpc("session.submit", {"thread_id": first, "text": "write first"})
                while not any(m.get("method") == "ui.request" for m in messages):
                    messages.append(json.loads(await ws.recv()))
                assert calls, "Production owner must consume SDK before accepting interaction"
                assert (await rpc("session.submit", {"thread_id": first,
                    "text": "Keep the requested file content unchanged"}))["ok"]
                from voidx.persistence.sqlite import fetch_all
                guidance = await fetch_all("SELECT text FROM guidance_inbox WHERE target_session_id = ?", (first,))
                assert any("requested file content" in row["text"] for row in guidance)
                request = next(m["params"] for m in messages if m.get("method") == "ui.request")
                second = (await rpc("session.create", {"profile": "coding"}))["thread_id"]
                await rpc("session.submit", {"thread_id": second, "text": "write second"})
                await rpc("session.switch", {"thread_id": first})
                while len(calls) < 2:
                    await asyncio.sleep(.02)
                assert calls[0] is not calls[1], "Concurrent roots must not reuse an SDK owner"
                session = app._run_loop._gateway_session
                assert session._run_manager.actor(first).is_active
                assert session._run_manager.actor(second).is_active
                assert await rpc("session.respond", {"thread_id": second,
                    "request_id": request["request_id"], "value": "allow"}) == {"ok": False}
                assert await rpc("session.cancel", {"thread_id": second}) == {"ok": True}
                while session._run_manager.actor(second).is_active:
                    await asyncio.sleep(.02)
                assert calls[0]._active
                assert not calls[1]._active
                assert await rpc("session.respond", {"thread_id": first,
                    "request_id": request["request_id"], "value": "allow"}) == {"ok": True}
                while session._run_manager.actor(first).is_active:
                    await asyncio.sleep(.02)
                assert (tmp_path / "sdk-probe.txt").read_text() == "headless\n"
                await rpc("session.submit", {"thread_id": second, "text": "write again"})
                while len(calls) < 3:
                    await asyncio.sleep(.02)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert calls and all(not agent._active for agent in calls)

    assert all(actor.state.status != "failed" for actor in session._run_manager._actors.values())

@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["coding", "goal", "loop"])
async def test_production_websocket_submit_owns_sdk_run(tmp_path, monkeypatch, profile):
    from voidx.presentation.terminal import run_loop

    model = {"coding": FileRoundTripModel, "goal": ChildToolsModel,
             "loop": TwoLoopsModel}[profile]()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    urls, sdk_calls, semantic = [], [], []
    original_stream = VoidxAgent.stream

    async def observed_stream(self, prompt, **kwargs):
        sdk_calls.append((self, prompt, kwargs))
        async for event in original_stream(self, prompt, **kwargs):
            semantic.append(event)
            yield event

    monkeypatch.setattr(VoidxAgent, "stream", observed_stream)
    monkeypatch.setattr(run_loop, "emit_web_gateway_bootstrap", urls.append)
    app = build_agent_app(config, "local-test", settings=settings)
    task = asyncio.create_task(app.run(web=True, web_headless=True))
    messages = []
    failure = None
    try:
        async with asyncio.timeout(35):
            while not urls:
                if task.done():
                    await task
                await asyncio.sleep(.02)
            async with connect(urls[0]) as ws:
                serial = 0

                async def rpc(method, params):
                    nonlocal serial
                    serial += 1
                    await ws.send(json.dumps(dict(jsonrpc="2.0", id=serial, method=method, params=params)))
                    while True:
                        message = json.loads(await ws.recv())
                        messages.append(message)
                        if message.get("id") == serial:
                            assert "error" not in message, message
                            return message["result"]

                created = await rpc("session.create", {"profile": profile})
                root = created["thread_id"]
                assert created["temporary"] is True
                assert (await rpc("session.submit", {"thread_id": root, "text": "Run production SDK probe"}))["ok"]
                while not any(m.get("method") == "ui.request" for m in messages):
                    messages.append(json.loads(await ws.recv()))
                assert sdk_calls, "Production session.submit still dispatches legacy runtime instead of owned SDK stream"
                assert sdk_calls[0][2]["session_id"] == root
                assert semantic and semantic[0].session_id == root
                request = next(m["params"] for m in messages if m.get("method") == "ui.request")
                assert request["thread_id"] == root
                assert (await rpc("session.respond", {"request_id": request["request_id"],
                    "thread_id": "wrong-root", "value": "allow" if profile == "coding" else "approved"})) == {"ok": False}
                assert (await rpc("session.respond", {"request_id": request["request_id"],
                    "thread_id": root, "value": "allow" if profile == "coding" else "approved"})) == {"ok": True}
                if profile == "coding":
                    while not any(e.kind == "turn.completed" for e in semantic):
                        await asyncio.sleep(.02)
                    assert (tmp_path / "sdk-probe.txt").read_text() == "headless\n"
                else:
                    while not any(e.kind == "turn.started" and e.thread_id != root for e in semantic):
                        await asyncio.sleep(.02)
                    session = app._run_loop._gateway_session
                    assert session._run_manager.actor(root).is_active, "Intake completion released run admission"
                    if profile == "goal":
                        while not any(m.get("method") == "ui.request" and m["params"]["thread_id"] != root for m in messages):
                            messages.append(json.loads(await ws.recv()))
                        child_request = next(m["params"] for m in messages
                            if m.get("method") == "ui.request" and m["params"]["thread_id"] != root)
                        assert await rpc("session.respond", {"request_id": child_request["request_id"],
                            "thread_id": root, "value": "allow"}) == {"ok": False}
                        assert await rpc("session.respond", {"request_id": child_request["request_id"],
                            "thread_id": child_request["thread_id"], "value": "allow"}) == {"ok": True}
                assert await rpc("session.cancel", {"thread_id": root}) == {"ok": True}
                host = app._run_loop._gateway_session._interaction_router
                while host.tasks:
                    await asyncio.sleep(.02)
                assert not host.owners, "Completed roots retain full SDK bridges"
                from voidx.bootstrap.production_sdk_gateway import ProductionSdkGateway
                restored = ProductionSdkGateway(host.session, host.legacy_handler,
                    config=config, settings=settings)
                await restored.restore()
                from voidx.agent.adapters.persistence.session_adapter import SessionRepositoryAdapter
                bindings = await SessionRepositoryAdapter().semantic_thread_bindings(str(tmp_path))
                assert bindings
                for tid, sid in bindings.items():
                    assert restored.session_id(tid) == sid
                    host.session._active_thread_id = tid
                    snapshot = await host.session._active_thread_snapshot()
                    assert snapshot.nodes, "Retired owner history must remain accessible"
                await restored.aclose()
    except BaseException as error:
        failure = error
        raise
    finally:
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        except Exception as cleanup_error:
            if failure is None:
                raise
            failure.add_note(f"Production shutdown also failed: {cleanup_error!r}")
    assert all(not agent._active for agent, _, _ in sdk_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_failure", [False, True])
async def test_production_transport_failure_cancels_without_awaiting_owner(tmp_path, monkeypatch, close_failure):
    from voidx.presentation.terminal import run_loop

    model = FileRoundTripModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    urls = []
    monkeypatch.setattr(run_loop, "emit_web_gateway_bootstrap", urls.append)
    app = build_agent_app(config, "local-test", settings=settings)
    task = asyncio.create_task(app.run(web=True, web_headless=True))
    self_waits = []
    gather = asyncio.gather

    def guarded_gather(*awaitables, **kwargs):
        # Fail fast instead of letting a self-referential gather deadlock shutdown.
        if asyncio.current_task() in awaitables:
            self_waits.append(asyncio.current_task())
            raise RuntimeError("Owner attempted to await itself")
        return gather(*awaitables, **kwargs)

    monkeypatch.setattr(asyncio, "gather", guarded_gather)
    try:
        async with asyncio.timeout(35):
            while not urls:
                if task.done():
                    await task
                await asyncio.sleep(.02)
            async with connect(urls[0]) as ws:
                async def rpc(ident, method, params):
                    await ws.send(json.dumps(dict(jsonrpc="2.0", id=ident, method=method, params=params)))
                    while True:
                        message = json.loads(await ws.recv())
                        if message.get("id") == ident:
                            assert "error" not in message
                            return message["result"]
                root = (await rpc(1, "session.create", {"profile": "coding"}))["thread_id"]
                await rpc(2, "session.submit", {"thread_id": root, "text": "write probe"})
                session = app._run_loop._gateway_session
                host = session._interaction_router
                while not host.requests():
                    await asyncio.sleep(.02)
                owner_task = host.tasks[root]
                agent = host.owners[root].agent
                if close_failure:
                    original_close = agent.aclose

                    async def broken_close():
                        await original_close()
                        raise RuntimeError("production close probe")

                    monkeypatch.setattr(agent, "aclose", broken_close)
                client = next(iter(session._clients))
                original_send = type(client).send_text
                failures = []

                async def broken_send(self, text, **kwargs):
                    if self is client and asyncio.current_task() is owner_task:
                        failures.append(text)
                        raise ConnectionError("production transport probe")
                    return await original_send(self, text, **kwargs)

                monkeypatch.setattr(type(client), "send_text", broken_send)
                request = host.requests()[0]
                await rpc(3, "session.respond", {"thread_id": root,
                    "request_id": request.request_id, "value": "allow"})
                result = await gather(owner_task, return_exceptions=True)
                assert failures, "Probe must exercise an owner-originated transport failure"
                assert not self_waits, "Transport cancellation must not await its calling owner"
                assert not agent._active
                assert not host.tasks and not host.owners
                assert not session._managed_run_threads
                assert not host.requests()
                assert isinstance(result[0], BaseException), "Transport cleanup errors must remain observable"
                if close_failure:
                    def leaves(error):
                        if isinstance(error, BaseExceptionGroup):
                            return [leaf for child in error.exceptions for leaf in leaves(child)]
                        return [error]
                    errors = leaves(result[0])
                    assert any("production close probe" in str(error) for error in errors)
                    assert any("production transport probe" in str(error) for error in errors)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
