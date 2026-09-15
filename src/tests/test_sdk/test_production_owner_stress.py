"""Production WS owners: deterministic read-only concurrency, not an RSS soak."""
import asyncio
import gc
import json
import weakref
from contextlib import asynccontextmanager, suppress

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from pydantic import PrivateAttr
from websockets.asyncio.client import connect

from tests.test_sdk.test_production_gateway_submission import (
    Config, PermissionMode, Settings, build_agent_app, langchain_model_factory,
)
from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.adapters.persistence.headless_locks import (
    HeadlessFileLock, HeadlessWorkspaceWriteLock,
)
from voidx.persistence.file_lock import acquire_file_lock_sync, release_file_lock_sync


class ReadBarrierModel(FileRoundTripModel):
    _ready: asyncio.Queue = PrivateAttr(default_factory=asyncio.Queue)
    _release: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    async def _astream(self, messages, **kwargs):
        reads = [m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "stress-read"]
        if reads:
            assert "OWNER_STRESS_EVIDENCE" in str(reads[-1].content), reads[-1]
            await self._ready.put(str(reads[-1].content))
            await self._release.wait()
            reply = AIMessageChunk(content="READ_COMPLETED")
        else:
            reply = AIMessageChunk(content="", tool_calls=[{
                "id": "stress-read", "name": "read",
                "args": {"file_path": "owner-stress.txt"}, "type": "tool_call",
            }])
        yield ChatGenerationChunk(message=reply)


@asynccontextmanager
async def production(tmp_path, monkeypatch):
    from voidx.presentation.terminal import run_loop

    (tmp_path / "owner-stress.txt").write_text("OWNER_STRESS_EVIDENCE\n")
    model = ReadBarrierModel()
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
    try:
        async with asyncio.timeout(90):
            while not urls:
                if task.done():
                    await task
                await asyncio.sleep(.01)
            async with connect(urls[0]) as ws:
                serial = 0

                async def rpc(method, params):
                    nonlocal serial
                    serial += 1
                    await ws.send(json.dumps(dict(jsonrpc="2.0", id=serial, method=method, params=params)))
                    while True:
                        message = json.loads(await ws.recv())
                        if message.get("id") == serial:
                            assert "error" not in message, message
                            return message["result"]

                session = app._run_loop._gateway_session
                yield model, session, session._interaction_router, rpc
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def start_read(model, host, rpc):
    root = (await rpc("session.create", {"profile": "coding"}))["thread_id"]
    await rpc("session.submit", {"thread_id": root, "text": "Read owner-stress.txt only; do not write."})
    evidence = await model._ready.get()
    assert "OWNER_STRESS_EVIDENCE" in evidence
    assert host.owners[root].agent._active
    assert not host.tasks[root].done()
    return root


def released(session, host, roots, tasks):
    assert all(task.done() for task in tasks)
    assert not host.tasks and not host.owners and not host._guidance
    assert not host.requests()
    assert not session._managed_run_threads
    assert not session._managed_terminal_statuses
    for root in roots:
        actor = session._run_manager.actor(root)
        assert not actor.is_active
        assert actor.state.status in {"idle", "failed", "cancelled", "completed"}


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]


@pytest.mark.asyncio
async def test_two_owner_send_failures_do_not_join_self_or_peer(tmp_path, monkeypatch):
    async with production(tmp_path, monkeypatch) as (model, session, host, rpc):
        roots = [await start_read(model, host, rpc) for _ in range(2)]
        tasks = [host.tasks[root] for root in roots]
        agents = [host.owners[root].agent for root in roots]
        assert agents[0] is not agents[1]
        client = next(iter(session._clients))
        send = type(client).send_text
        gather = asyncio.gather
        arrived, cancel_callers, joins = set(), set(), []
        barrier = asyncio.Event()
        cancel_all = host.cancel_all

        async def observed_cancel():
            cancel_callers.add(asyncio.current_task())
            await cancel_all()

        def guarded_gather(*items, **kwargs):
            if asyncio.current_task() in tasks and any(item in tasks for item in items):
                joins.append(asyncio.current_task())
                raise AssertionError("owner cancellation attempted self/peer join")
            return gather(*items, **kwargs)

        async def broken_send(self, text, **kwargs):
            caller = asyncio.current_task()
            if self is client and caller in tasks:
                arrived.add(caller)
                if len(arrived) == 2:
                    barrier.set()
                await barrier.wait()
                raise ConnectionError(f"stress-send-{tasks.index(caller)}")
            return await send(self, text, **kwargs)

        monkeypatch.setattr(session, "_interaction_cancel", observed_cancel)
        monkeypatch.setattr(asyncio, "gather", guarded_gather)
        monkeypatch.setattr(type(client), "send_text", broken_send)
        model._release.set()
        results = await gather(*tasks, return_exceptions=True)
        assert arrived == set(tasks)
        assert set(tasks) <= cancel_callers
        assert not joins
        for index, result in enumerate(results):
            assert any(f"stress-send-{index}" in str(e) for e in leaves(result)), results
        assert all(not agent._active for agent in agents)
        released(session, host, roots, tasks)


@pytest.mark.asyncio
async def test_two_owner_cancel_and_close_errors_are_all_aggregated(tmp_path, monkeypatch):
    async with production(tmp_path, monkeypatch) as (model, session, host, rpc):
        roots = [await start_read(model, host, rpc) for _ in range(2)]
        tasks = [host.tasks[root] for root in roots]
        agents = [host.owners[root].agent for root in roots]
        called = []
        for index, agent in enumerate(agents):
            for method in ("cancel", "aclose"):
                original = getattr(agent, method)
                marker = f"stress-{method}-{index}"

                async def fault(original=original, marker=marker):
                    await original()
                    called.append(marker)
                    raise RuntimeError(marker)

                monkeypatch.setattr(agent, method, fault)
        with pytest.raises(BaseExceptionGroup) as caught:
            await host.cancel_all()
        expected = {f"stress-{method}-{index}" for index in range(2) for method in ("cancel", "aclose")}
        assert set(called) == expected
        errors = leaves(caught.value)
        assert expected <= {str(error) for error in errors}, errors
        assert all(not agent._active for agent in agents)
        released(session, host, roots, tasks)


@pytest.mark.asyncio
async def test_twelve_real_root_lifecycles_release_bridges_tasks_and_locks(tmp_path, monkeypatch, record_property):
    references, roots, completed, cancelled = [], [], 0, 0
    async with production(tmp_path, monkeypatch) as (model, session, host, rpc):
        for index in range(12):
            model._release.clear()
            root = await start_read(model, host, rpc)
            roots.append(root)
            references.append(weakref.ref(host.owners[root]))
            task = host.tasks[root]
            if index % 2:
                assert await rpc("session.cancel", {"thread_id": root}) == {"ok": True}
                cancelled += 1
            else:
                model._release.set()
                completed += 1
            await task
            released(session, host, roots, [task])
            del task
            await asyncio.sleep(0)
            gc.collect()
            assert all(ref() is None for ref in references), f"bridge retained at cycle {index}"
            for lock in (HeadlessFileLock("session", root), HeadlessWorkspaceWriteLock(str(tmp_path))):
                handle = acquire_file_lock_sync(lock._path, blocking=False)
                release_file_lock_sync(handle)
            pending = [t for t in asyncio.all_tasks() if not t.done()
                       and "ProductionSdkGateway._run" in t.get_coro().__qualname__]
            assert not pending, pending
        assert len(set(roots)) == 12
        assert completed == cancelled == 6
        record_property("cycles", 12)
        record_property("completed", completed)
        record_property("cancelled", cancelled)
        record_property("collected_bridges", sum(ref() is None for ref in references))
        record_property("lock_reacquisitions", 24)
