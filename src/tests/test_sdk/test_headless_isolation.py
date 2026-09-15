"""Isolation through the real SDK graph and file tools (only the model is fake)."""
import asyncio
from contextlib import aclosing
from pathlib import Path

import pytest

from test_headless_runtime import FileRoundTripModel
from voidx.config import Config, PermissionMode, Settings
from voidx.sdk import VoidxAgent


@pytest.fixture
def factory(tmp_path, monkeypatch):
    from voidx.llm.adapters import langchain_model_factory
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **k: FileRoundTripModel())
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **k: FileRoundTripModel())
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED,
                    lsp_format_after_edit=False)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, "local-test-not-a-credential")
    return lambda: VoidxAgent(config, settings=settings)


async def consume(factory, workspace, session_id="", prompt="write probe"):
    async with factory() as agent:
        async with aclosing(agent.stream(prompt, workspace=str(workspace), session_id=session_id)) as stream:
            return [event async for event in stream]


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["absolute", "relative", "symlink", "different"])
async def test_workspace_writes(factory, tmp_path, monkeypatch, alias):
    from voidx.tooling.builtin.file.write import WriteTool
    first = tmp_path / "first"
    first.mkdir()
    second = first
    if alias == "relative":
        monkeypatch.chdir(tmp_path)
        second = Path("first")
    elif alias == "symlink":
        second = tmp_path / "link"
        second.symlink_to(first, target_is_directory=True)
    elif alias == "different":
        second = tmp_path / "second"
        second.mkdir()
    entered = asyncio.Event()
    other_entered = asyncio.Event()
    release = asyncio.Event()
    original = WriteTool.execute
    count = 0

    async def blocked(self, args, ctx):
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            await release.wait()
        else:
            other_entered.set()
        return await original(self, args, ctx)

    monkeypatch.setattr(WriteTool, "execute", blocked)
    a = asyncio.create_task(consume(factory, first))
    b = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        b = asyncio.create_task(consume(factory, second))
        if alias == "different":
            await asyncio.wait_for(other_entered.wait(), 10)
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(other_entered.wait(), 1)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(a, b), 15)
        assert all(events[-1].kind == "turn.completed" for events in results)
        assert (first / "sdk-probe.txt").read_text() == "headless\n"
    finally:
        release.set()
        await asyncio.gather(*(t for t in (a, b) if t), return_exceptions=True)


@pytest.mark.asyncio
async def test_session_workspace_mismatch_before_started(factory, tmp_path):
    events = await consume(factory, tmp_path, prompt="RESTORE_PROBE")
    other = tmp_path / "other"
    other.mkdir()
    async with factory() as agent:
        async with aclosing(agent.stream("wrong", workspace=str(other), session_id=events[0].session_id)) as stream:
            with pytest.raises(ValueError, match="workspace"):
                await anext(stream)


@pytest.mark.asyncio
async def test_same_session_serializes_restore(factory, tmp_path, monkeypatch):
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution
    from voidx.agent.adapters.persistence.session_repository import load_messages
    seed = await consume(factory, tmp_path, prompt="RESTORE_PROBE seed")
    sid = seed[0].session_id
    original = LangGraphExecution.restore_runtime_state
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def blocked(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(self)

    monkeypatch.setattr(LangGraphExecution, "restore_runtime_state", blocked)
    a = asyncio.create_task(consume(factory, tmp_path, sid, "RESTORE_PROBE first"))
    b = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        b = asyncio.create_task(consume(factory, tmp_path, sid, "RESTORE_PROBE second"))
        await asyncio.sleep(1)
        assert calls == 1
        release.set()
        results = await asyncio.wait_for(asyncio.gather(a, b), 15)
        assert all(r[-1].kind == "turn.completed" for r in results)
        rows = await load_messages(sid)
        text = str(rows)
        assert "RESTORE_PROBE first" in text and "RESTORE_PROBE second" in text
    finally:
        release.set()
        await asyncio.gather(*(t for t in (a, b) if t), return_exceptions=True)


@pytest.mark.asyncio
async def test_initialization_failure_releases_session(factory, tmp_path, monkeypatch):
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution
    seed = await consume(factory, tmp_path, prompt="RESTORE_PROBE")
    original = LangGraphExecution.restore_runtime_state

    async def fail(self):
        raise ValueError("restore failed")

    monkeypatch.setattr(LangGraphExecution, "restore_runtime_state", fail)
    with pytest.raises(ValueError, match="restore failed"):
        await consume(factory, tmp_path, seed[0].session_id)
    monkeypatch.setattr(LangGraphExecution, "restore_runtime_state", original)
    events = await asyncio.wait_for(consume(factory, tmp_path, seed[0].session_id), 15)
    assert events[-1].kind == "turn.completed"


@pytest.mark.asyncio
async def test_cancelled_lock_waiter_has_no_handle_or_task_leak(tmp_path):
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock
    holder = HeadlessFileLock("workspace", str(tmp_path))
    waiter = HeadlessFileLock("workspace", str(tmp_path))
    await holder.acquire()
    task = asyncio.create_task(waiter.acquire())
    try:
        await asyncio.sleep(0.1)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert waiter._handle is None
    finally:
        holder.release()
    await asyncio.wait_for(waiter.acquire(), 2)
    waiter.release()
    assert holder._handle is None and waiter._handle is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_holder", [True, False])
async def test_sdk_cancel_releases_workspace_and_session(factory, tmp_path, monkeypatch, cancel_holder):
    from voidx.tooling.builtin.file.write import WriteTool
    entered, release = asyncio.Event(), asyncio.Event()
    original = WriteTool.execute
    count = 0

    async def blocked(self, args, ctx):
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            await release.wait()
        return await original(self, args, ctx)

    monkeypatch.setattr(WriteTool, "execute", blocked)
    holder, waiter = factory(), factory()
    seen = [[], []]

    async def run(agent, index):
        async with aclosing(agent.stream("write", workspace=str(tmp_path))) as stream:
            async for event in stream:
                seen[index].append(event)

    a = asyncio.create_task(run(holder, 0))
    b = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        b = asyncio.create_task(run(waiter, 1))
        async with asyncio.timeout(10):
            while not any(event.kind == "tool.started" for event in seen[1]):
                await asyncio.sleep(0.01)
        assert count == 1
        await asyncio.wait_for((holder if cancel_holder else waiter).cancel(), 5)
        release.set()
        await asyncio.wait_for(asyncio.gather(a, b), 10)
        cancelled = seen[0 if cancel_holder else 1]
        assert cancelled[-1].kind == "turn.cancelled"
        for events in seen:
            resumed = await asyncio.wait_for(consume(factory, tmp_path, events[0].session_id,
                                                    "RESTORE_PROBE after cancellation"), 10)
            assert resumed[-1].kind == "turn.completed"
    finally:
        release.set()
        await asyncio.gather(*(t for t in (a, b) if t), return_exceptions=True)
        await holder.aclose()
        await waiter.aclose()


@pytest.mark.asyncio
async def test_cancel_initialization_releases_session(factory, tmp_path, monkeypatch):
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution
    seed = await consume(factory, tmp_path, prompt="RESTORE_PROBE")
    sid = seed[0].session_id
    entered = asyncio.Event()
    original = LangGraphExecution.restore_runtime_state

    async def blocked(self):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(LangGraphExecution, "restore_runtime_state", blocked)
    task = asyncio.create_task(consume(factory, tmp_path, sid))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(LangGraphExecution, "restore_runtime_state", original)
    result = await asyncio.wait_for(consume(factory, tmp_path, sid), 10)
    assert result[-1].kind == "turn.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "session"])
async def test_os_lock_cross_process(tmp_path, kind):
    import os
    import sys
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock
    from voidx.persistence import sqlite as store

    lock = HeadlessFileLock(kind, str(tmp_path))
    await lock.acquire()
    code = """
import asyncio, sys
from pathlib import Path
from voidx.persistence import sqlite as store
from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock
store.DATA_DIR = Path(sys.argv[1])
async def main():
    lock = HeadlessFileLock(sys.argv[2], sys.argv[3])
    print('ready', flush=True)
    await lock.acquire()
    print('acquired', flush=True)
    lock.release()
asyncio.run(main())
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(store.DATA_DIR), kind, str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd=tmp_path,
    )
    try:
        ready = await asyncio.wait_for(process.stdout.readline(), 10)
        assert ready == b"ready\n", (await process.stderr.read()).decode() if not ready else ready
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(process.stdout.readline(), 0.2)
        lock.release()
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        assert stdout == b"acquired\n"
    finally:
        lock.release()
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_lock_filesystem_errors_are_not_retried(tmp_path, monkeypatch):
    import errno
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock

    def denied(self, *args, **kwargs):
        raise PermissionError(errno.EACCES, "lock directory denied")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(PermissionError, match="lock directory denied"):
        await asyncio.wait_for(HeadlessFileLock("session", "test").acquire(), 0.2)


@pytest.mark.asyncio
async def test_waiting_session_refreshes_metadata(factory, tmp_path, monkeypatch):
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock
    from voidx.agent.adapters.persistence.session_repository import update_title
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution
    seed = await consume(factory, tmp_path, prompt="RESTORE_PROBE")
    sid = seed[0].session_id
    lease = HeadlessFileLock("session", sid)
    await lease.acquire()
    original = LangGraphExecution.restore_runtime_state
    titles = []

    async def restore(self):
        titles.append(self._session.title)
        return await original(self)

    monkeypatch.setattr(LangGraphExecution, "restore_runtime_state", restore)
    task = asyncio.create_task(consume(factory, tmp_path, sid, "RESTORE_PROBE"))
    try:
        await asyncio.sleep(0.2)
        await update_title(sid, "updated by preceding owner")
    finally:
        lease.release()
    result = await asyncio.wait_for(task, 10)
    assert result[-1].kind == "turn.completed"
    assert titles == ["updated by preceding owner"]
