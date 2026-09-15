"""Root guidance must reach real autonomous work through production WebSocket."""
import asyncio
import json
import threading
from contextlib import suppress

import pytest
from pydantic import PrivateAttr
from websockets.asyncio.client import connect

from tests.test_sdk.test_autonomous_gateway_projection import TwoLoopsModel
from tests.test_sdk.test_autonomous_session_tools import ChildToolsModel
from voidx.bootstrap.agent import build_agent_app
from voidx.config import Config, PermissionMode, Settings
from voidx.llm.adapters import langchain_model_factory
from voidx.persistence.sqlite import fetch_all


class HeldGoalModel(ChildToolsModel):
    _held: threading.Event = PrivateAttr(default_factory=threading.Event)
    _release: threading.Event = PrivateAttr(default_factory=threading.Event)

    def _reply(self, messages):
        if self._step == 5:
            self._held.set()
            assert self._release.wait(30), "Evaluator hold was not released"
        return super()._reply(messages)


class HeldLoopModel(TwoLoopsModel):
    _held: threading.Event = PrivateAttr(default_factory=threading.Event)
    _release: threading.Event = PrivateAttr(default_factory=threading.Event)

    def _reply(self, messages):
        self._histories.append(list(messages))
        if self._step == 3:
            self._held.set()
            assert self._release.wait(30), "Loop commit hold was not released"
        return super()._reply(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["goal", "loop"])
async def test_active_root_guidance_reaches_next_autonomous_work(tmp_path, monkeypatch, profile):
    await _exercise_guidance(tmp_path, monkeypatch, profile)


async def _exercise_guidance(tmp_path, monkeypatch, profile, *, lifecycle=False, intake=False, closing=False):
    from voidx.presentation.terminal import run_loop

    model = HeldGoalModel() if profile == "goal" else HeldLoopModel()
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
    marker = "S6_ROOT_GUIDANCE_USE_COBALT_EVIDENCE"
    messages = []
    answered = set()
    failure = None
    try:
        async with asyncio.timeout(60):
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

                async def progress():
                    for message in list(messages):
                        if message.get("method") != "ui.request":
                            continue
                        request = message["params"]
                        if request["request_id"] in answered:
                            continue
                        answered.add(request["request_id"])
                        assert await rpc("session.respond", {
                            "thread_id": request["thread_id"], "request_id": request["request_id"],
                            "value": "approved" if request["thread_id"] in roots else "allow",
                        }) == {"ok": True}
                    try:
                        messages.append(json.loads(await asyncio.wait_for(ws.recv(), .05)))
                    except asyncio.TimeoutError:
                        pass

                root = (await rpc("session.create", {"profile": profile}))["thread_id"]
                roots = {root}
                assert (await rpc("session.submit", {"thread_id": root, "text": "Run two evidence iterations"}))["ok"]
                if intake:
                    while not any(m.get("method") == "ui.request" for m in messages):
                        messages.append(json.loads(await ws.recv()))
                    intake_marker = "INTAKE_APPROVAL_GUIDANCE"
                    assert (await rpc("session.submit", {"thread_id": root, "text": intake_marker}))["ok"]
                    intake_row = (await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (intake_marker,)))[0]
                    assert intake_row["target_session_id"] == root
                    assert intake_row["target_thread_id"] == root
                    assert intake_row["delivery_id"]
                while not model._held.is_set():
                    await progress()
                session = app._run_loop._gateway_session
                assert session._run_manager.actor(root).is_active
                assert (await rpc("session.submit", {"thread_id": root, "text": marker}))["ok"]
                if intake:
                    intake_row = (await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (intake_marker,)))[0]
                    if profile == "goal":
                        assert intake_row["consumed_at"] is None
                        assert intake_row["delivery_id"] is None
                        assert intake_row["target_phase"] == "idle"
                        assert all(intake_marker not in str(m.content) for h in model._histories[1:] for m in h)
                    else:
                        assert intake_row["consumed_at"] is not None
                        assert any(intake_marker in str(m.content) for h in model._histories for m in h)
                pending = await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (marker,))
                assert len(pending) == 1 and pending[0]["consumed_at"] is None
                from voidx.agent.adapters.persistence.thread_repository import ThreadStore
                from voidx.agent.application.guidance_service import GuidanceService
                service = GuidanceService(ThreadStore())
                target = pending[0]
                other = (await rpc("session.create", {"profile": "coding" if lifecycle else profile}))["thread_id"]
                roots.add(other)
                assert await service.bind_delivery("other-root-probe", session_id=other, phase="work") == []
                if profile == "goal":
                    for generation in ("previous-generation", "next-generation"):
                        assert await service.bind_delivery(
                            generation, session_id=target["target_session_id"],
                            thread_id=target["target_thread_id"], run_id=generation, phase="work",
                        ) == []
                    assert await service.bind_delivery(
                        "evaluator-probe", session_id=target["target_session_id"],
                        thread_id=target["target_thread_id"], run_id=target["target_run_id"],
                        phase="evaluator",
                    ) == []
                if lifecycle:
                    from langchain_core.messages import AIMessage
                    class OtherRootModel(ChildToolsModel):
                        def _reply(self, messages):
                            self._histories.append(list(messages))
                            return AIMessage(content="Other root provider completed independently.")
                    other_model = OtherRootModel()
                    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: other_model)
                    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: other_model)
                    assert (await rpc("session.submit", {"thread_id": other, "text": "Run other root evidence"}))["ok"]
                    while session._run_manager.actor(other).is_active:
                        await progress()
                    assert len(other_model._histories) >= 1
                    assert all(marker not in str(m.content) for h in other_model._histories for m in h)
                    pending_other = await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (marker,))
                    assert pending_other[0]["consumed_at"] is None
                    assert session._run_manager.actor(root).is_active
                    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
                    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
                if closing:
                    router = session._interaction_router
                    old_owner = router.owners[root]
                    old_task = router.tasks[root]
                    old_capability = router._guidance[root]
                    owner_closed = asyncio.Event()
                    snapshot_held = asyncio.Event()
                    snapshot_release = asyncio.Event()
                    original_close = old_owner.agent.aclose
                    original_snapshot = session.broadcast_snapshot

                    async def close_owner():
                        await original_close()
                        owner_closed.set()

                    async def hold_final_snapshot(*args, **kwargs):
                        if asyncio.current_task() is old_task and owner_closed.is_set():
                            snapshot_held.set()
                            await snapshot_release.wait()
                        return await original_snapshot(*args, **kwargs)

                    monkeypatch.setattr(old_owner.agent, "aclose", close_owner)
                    monkeypatch.setattr(session, "broadcast_snapshot", hold_final_snapshot)
                model._release.set()
                next_work = 7 if profile == "goal" else 4
                while len(model._histories) <= next_work + 1:
                    await progress()
                history = model._histories[next_work]
                count = sum(marker in str(message.content) for message in history)
                assert count == 1, f"Next real {profile} work provider history guidance count={count}"
                assert all(marker not in str(message.content)
                           for history in model._histories[:next_work] for message in history), "Intake/evaluator consumed work guidance"
                while True:
                    consumed = await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (marker,))
                    if consumed[0]["consumed_at"] is not None:
                        break
                    await progress()
                assert len(consumed) == 1
                assert consumed[0]["delivery_id"]
                assert consumed[0]["delivered_phase"] == "work"
                assert await service.bind_delivery(
                    "replay-probe", session_id=target["target_session_id"],
                    thread_id=target["target_thread_id"], run_id=target["target_run_id"] or "",
                    phase="work",
                ) == []
                from voidx.agent.adapters.persistence.session_repository import load_messages
                store = ThreadStore()
                if profile == "goal":
                    binding = (await store.list_goal_generations(root))[-1]
                    child_session = binding.work_session_id
                    evaluator = await load_messages(binding.evaluator_session_id)
                    assert all(marker not in str(row.content) for row in evaluator)
                else:
                    child_session = target["target_session_id"]
                persisted = await load_messages(child_session)
                assert sum(marker in str(row.content) for row in persisted) == 1
                assert all(marker not in str(row.content) for row in await load_messages(other))
                if closing:
                    while not snapshot_held.is_set():
                        await progress()
                    assert not old_task.done()
                    assert session._run_manager.actor(root).is_active, "Root admission released before final owner snapshot completed"
                    closing_marker = "FINAL_SNAPSHOT_OLD_GENERATION_GUIDANCE"
                    assert (await rpc("session.submit", {"thread_id": root, "text": closing_marker}))["ok"]
                    closing_row = (await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (closing_marker,)))[0]
                    assert closing_row["target_run_id"] == binding.generation
                    assert closing_row["target_session_id"] == binding.work_session_id
                    assert closing_row["target_thread_id"] == binding.goal_thread_id
                    assert closing_row["target_phase"] == "work"
                    assert len(await store.list_goal_generations(root)) == 1
                    assert router.owners[root] is old_owner
                    assert router.tasks[root] is old_task
                    assert router._guidance[root] is old_capability
                    snapshot_release.set()
                    await old_task
                    assert root not in router.tasks and root not in router.owners and root not in router._guidance
                    while True:
                        try:
                            messages.append(json.loads(await asyncio.wait_for(ws.recv(), .1)))
                        except asyncio.TimeoutError:
                            break
                    root_statuses = [
                        thread["status"]
                        for message in messages
                        if message.get("method") == "workspace.snapshot"
                        for thread in message["params"]["threads"]
                        if thread["thread_id"] == root
                    ]
                    assert root_statuses[-1] == "idle", root_statuses
                if lifecycle:
                    while session._run_manager.actor(root).is_active:
                        await progress()
                    old_generation = binding.generation
                    from voidx.agent.domain.guidance import Guidance
                    stale = await store.submit_guidance(Guidance(
                        guidance_id="old-generation-undelivered", text="OLD_GENERATION_ONLY",
                        target_session_id=target["target_session_id"],
                        target_thread_id=target["target_thread_id"],
                        target_run_id=old_generation, target_phase="work"))
                    fresh = HeldGoalModel()
                    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: fresh)
                    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: fresh)
                    assert (await rpc("session.submit", {"thread_id": root, "text": "Start a new goal generation"}))["ok"]
                    while not fresh._held.is_set():
                        await progress()
                    new_marker = "NEW_GENERATION_ONLY"
                    assert (await rpc("session.submit", {"thread_id": root, "text": new_marker}))["ok"]
                    generations = await store.list_goal_generations(root)
                    assert len(generations) == 2
                    assert generations[-1].generation != old_generation
                    new_row = (await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (new_marker,)))[0]
                    assert new_row["target_run_id"] == generations[-1].generation, json.dumps(dict(new_row))
                    if closing:
                        assert new_row["target_session_id"] == generations[-1].work_session_id
                        assert new_row["target_thread_id"] == generations[-1].goal_thread_id
                        assert new_row["target_phase"] == "work"
                    fresh._release.set()
                    while session._run_manager.actor(root).is_active:
                        await progress()
                    assert sum(new_marker in str(m.content) for m in fresh._histories[7]) == 1
                    assert all("OLD_GENERATION_ONLY" not in str(m.content) and marker not in str(m.content)
                               for h in fresh._histories for m in h)
                    assert all(new_marker not in str(m.content) for h in fresh._histories[10:] for m in h)
                    old_row = (await fetch_all("SELECT * FROM guidance_inbox WHERE guidance_id = ?", (stale.guidance_id,)))[0]
                    assert old_row["consumed_at"] is None and old_row["delivery_id"] is None
                    if closing:
                        delivered = (await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (new_marker,)))[0]
                        assert delivered["consumed_at"] is not None and delivered["delivery_id"]
                        assert delivered["delivered_phase"] == "work"
                        assert all(closing_marker not in str(m.content) for h in fresh._histories for m in h)
                        closing_row = (await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (closing_marker,)))[0]
                        assert closing_row["consumed_at"] is None and closing_row["delivery_id"] is None
                assert await rpc("session.cancel", {"thread_id": root}) == {"ok": True}
    except BaseException as error:
        failure = error
        raise
    finally:
        if "snapshot_release" in locals():
            snapshot_release.set()
        model._release.set()
        if "fresh" in locals():
            fresh._release.set()
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        except Exception as cleanup_error:
            if failure is None:
                raise
            failure.add_note(f"Production shutdown also failed: {cleanup_error!r}")


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["goal", "loop"])
async def test_guidance_uses_live_owner_not_persisted_prefix(tmp_path, monkeypatch, profile):
    import inspect
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore

    original = ThreadStore.latest_thread_id_with_prefix

    async def reject_gateway_guess(store, prefix):
        assert not any(frame.function == "handle" and
                       frame.filename.endswith("production_sdk_gateway.py")
                       for frame in inspect.stack()), "Guidance must use exact live owner identity, not a persisted prefix"
        return await original(store, prefix)

    monkeypatch.setattr(ThreadStore, "latest_thread_id_with_prefix", reject_gateway_guess)
    await test_active_root_guidance_reaches_next_autonomous_work(tmp_path, monkeypatch, profile)


@pytest.mark.asyncio
async def test_concurrent_other_root_and_real_goal_generation_rollover(tmp_path, monkeypatch):
    await _exercise_guidance(tmp_path, monkeypatch, "goal", lifecycle=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["goal", "loop"])
async def test_preapproval_guidance_uses_current_intake_delivery(tmp_path, monkeypatch, profile):
    await _exercise_guidance(tmp_path, monkeypatch, profile, intake=True)


class RecoveryEvaluatorModel(HeldGoalModel):
    def _reply(self, messages):
        from langchain_core.messages import AIMessage
        self._histories.append(list(messages))
        if self._step:
            return AIMessage(content="Recovered evidence verified.")
        self._step += 1
        self._held.set()
        assert self._release.wait(30), "Recovered evaluator was not released"
        return AIMessage(content="", tool_calls=[{
            "id": "recovered-decision", "name": "goal_decision",
            "args": {"status": "finished", "summary": "Evidence checked",
                     "progress": "meaningful", "evidence": ["work"]},
        }])


class RpcPeers:
    def __init__(self):
        self.sockets = []
        self.readers = []
        self.messages = []

    async def rpc(self, url, method, params):
        ws = await connect(url)
        self.sockets.append(ws)
        result = asyncio.get_running_loop().create_future()

        async def read():
            async for raw in ws:
                message = json.loads(raw)
                self.messages.append(message)
                if message.get("id") == 1 and not result.done():
                    result.set_result(message)

        self.readers.append(asyncio.create_task(read()))
        await ws.send(json.dumps(dict(jsonrpc="2.0", id=1, method=method, params=params)))
        return await result

    async def close(self):
        for ws in self.sockets:
            await ws.close()
        await asyncio.gather(*self.readers)


@pytest.mark.asyncio
async def test_recovered_checkpoint_first_evaluator_guidance_is_durable(tmp_path, monkeypatch):
    import subprocess
    import sys
    from voidx.agent.adapters.persistence.session_repository import create_session
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.presentation.terminal import run_loop

    session = await create_session(workspace=str(tmp_path), profile="goal")
    command = [sys.executable, "-c",
               "import asyncio,sys; from pathlib import Path; "
               "from tests.test_sdk.test_autonomous_goal_recovery_crash import _crash_process; "
               "asyncio.run(_crash_process(Path(sys.argv[1]),sys.argv[2],'checkpoint'))",
               str(tmp_path), session.id]
    process = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=40)
    assert process.returncode == 73, process.stdout + process.stderr
    store = ThreadStore()
    binding = (await store.list_goal_generations(session.id))[0]
    assert [(r.phase, r.status) for r in await store.list_goal_protocols(binding.generation)] == [
        ("init", "projected"), ("checkpoint", "submitted")]
    model = RecoveryEvaluatorModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE, lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    peers = RpcPeers()
    urls = []
    monkeypatch.setattr(run_loop, "emit_web_gateway_bootstrap", urls.append)
    app = build_agent_app(config, "local-test", settings=settings)
    task = asyncio.create_task(app.run(web=True, web_headless=True))
    marker = "RECOVERED_EVALUATOR_PENDING_WORK"
    try:
        async with asyncio.timeout(60):
            while not urls:
                await asyncio.sleep(.02)
            await peers.rpc(urls[0], "session.switch", {"thread_id": session.id})
            assert (await peers.rpc(urls[0], "session.submit", {
                "thread_id": session.id, "text": "Resume checkpoint"}))["result"]["ok"]
            host = app._run_loop._gateway_session._interaction_router
            owner_task = host.tasks.get(session.id)
            assert owner_task is not None
            while not model._held.is_set():
                if owner_task.done():
                    await owner_task
                    pytest.fail("Recovery finished without evaluator")
                await asyncio.sleep(.02)
            response = await asyncio.wait_for(peers.rpc(urls[0], "session.submit", {
                "thread_id": session.id, "text": marker}), 3)
            assert response["result"]["ok"], response
            row = (await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (marker,)))[0]
            assert (row["target_session_id"], row["target_thread_id"], row["target_run_id"], row["target_phase"]) == (
                binding.work_session_id, binding.goal_thread_id, binding.generation, "work")
            assert row["consumed_at"] is None
            assert all(marker not in str(m.content) for h in model._histories for m in h)
            model._release.set()
            host = app._run_loop._gateway_session._interaction_router
            answered = set()
            while session.id in host.tasks:
                for message in list(peers.messages):
                    if message.get("method") != "ui.request":
                        continue
                    request = message["params"]
                    if request["request_id"] in answered:
                        continue
                    answered.add(request["request_id"])
                    assert request["thread_id"] != session.id
                    assert (await peers.rpc(urls[0], "session.respond", {
                        "thread_id": request["thread_id"], "request_id": request["request_id"],
                        "value": "allow"}))["result"]["ok"]
                await asyncio.sleep(.02)
            assert (await store.load(binding.goal_thread_id)).state.lifecycle.value == "completed"
            from voidx.agent.application.guidance_service import GuidanceService
            assert await GuidanceService(store).bind_delivery("next-generation", session_id=binding.work_session_id,
                thread_id=binding.goal_thread_id, run_id="next-generation", phase="work") == []
            row = (await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", (marker,)))[0]
            assert row["consumed_at"] is None
    finally:
        model._release.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await peers.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "close"])
async def test_pre_first_execution_queued_guidance_terminates(tmp_path, monkeypatch, ending):
    from voidx.bootstrap import headless
    from voidx.presentation.terminal import run_loop

    entered = asyncio.Event()
    async def hold_execution(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(headless, "_build_execution", hold_execution)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test")
    peers = RpcPeers()
    urls = []
    monkeypatch.setattr(run_loop, "emit_web_gateway_bootstrap", urls.append)
    app = build_agent_app(config, "local-test", settings=settings)
    task = asyncio.create_task(app.run(web=True, web_headless=True))
    queued = None
    try:
        async with asyncio.timeout(20):
            while not urls:
                await asyncio.sleep(.02)
            root = (await peers.rpc(urls[0], "session.create", {"profile": "coding"}))["result"]["thread_id"]
            await peers.rpc(urls[0], "session.submit", {"thread_id": root, "text": "Start"})
            await entered.wait()
            host = app._run_loop._gateway_session._interaction_router
            capability = host._guidance[root]
            waiting = asyncio.Event()
            original_submit = capability.submit
            async def submit(text):
                waiting.set()
                return await original_submit(text)
            monkeypatch.setattr(capability, "submit", submit)
            queued = asyncio.create_task(peers.rpc(urls[0], "session.submit", {
                "thread_id": root, "text": "QUEUED_BEFORE_EXECUTION"}))
            await waiting.wait()
            assert not queued.done()
            if ending == "cancel":
                response = await asyncio.wait_for(peers.rpc(urls[0], "session.cancel", {"thread_id": root}), 3)
                assert "error" not in response, response
            else:
                await asyncio.wait_for(host.aclose(), 3)
            response = await asyncio.wait_for(queued, 3)
            assert "error" in response, response
            assert root not in host.tasks
            assert root not in host._guidance
            assert await fetch_all("SELECT * FROM guidance_inbox WHERE text = ?", ("QUEUED_BEFORE_EXECUTION",)) == []
    finally:
        if queued is not None:
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)
        if urls:
            host = app._run_loop._gateway_session._interaction_router
            for owner_task in tuple(host.tasks.values()):
                owner_task.cancel()
            await asyncio.gather(*tuple(host.tasks.values()), return_exceptions=True)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await peers.close()


@pytest.mark.asyncio
async def test_final_owner_snapshot_retains_admission_until_generation_cleanup(tmp_path, monkeypatch):
    await _exercise_guidance(tmp_path, monkeypatch, "goal", lifecycle=True, closing=True)
