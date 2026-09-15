"""Owner capability contracts with real OS locks (no lock mocks)."""
import asyncio
import subprocess
import sys

import pytest

from voidx.agent.adapters.persistence import headless_locks as locks
from voidx.persistence.file_lock import acquire_file_lock_sync, release_file_lock_sync


def owner(workspace):
    return locks.OwnedWorkspaceWriteLock(str(workspace))


def probe(workspace):
    return locks.HeadlessWorkspaceWriteLock(str(workspace))


def assert_os_locked(workspace):
    with pytest.raises(BlockingIOError):
        handle = acquire_file_lock_sync(probe(workspace)._path, blocking=False)
        release_file_lock_sync(handle)


async def queued(facade, thread_id):
    started = asyncio.Event()

    async def acquire():
        started.set()
        return await facade.acquire_workspace_write_lock(thread_id)

    task = asyncio.create_task(acquire())
    await started.wait()
    return task


@pytest.mark.asyncio
async def test_children_share_handle_but_serialize_and_parent_waits_without_gate(tmp_path):
    owned = owner(tmp_path)
    parent, first, second = owned.child("parent"), owned.child("a"), owned.child("b")
    external = probe(tmp_path)
    # Registering parent/children does not acquire a write lock at intake.
    await asyncio.wait_for(external.acquire(), 1)
    external.release()
    try:
        assert await first.acquire_workspace_write_lock("thread")
        handle = owned._lock._handle
        waiter = await queued(second, "thread")
        try:
            assert not waiter.done()
            # Neither a sibling nor a different thread can release this borrow.
            second.release_workspace_write_lock("thread")
            first.release_workspace_write_lock("wrong-thread")
            assert_os_locked(tmp_path)
            assert not waiter.done()
            first.release_workspace_write_lock("thread")
            assert await asyncio.wait_for(waiter, 1)
            assert owned._lock._handle is handle
            # Release on the acquiring task, as the execution's tool finally does.
            # This waiter already ended; owner close still cleans the borrow.
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
    finally:
        owned.close()
    assert handle[0].closed
    await asyncio.wait_for(external.acquire(), 1)
    external.release()


@pytest.mark.asyncio
async def test_child_release_retains_owner_os_handle_until_close(tmp_path):
    owned = owner(tmp_path)
    child = owned.child("child")
    try:
        await child.acquire_workspace_write_lock("t")
        handle = owned._lock._handle
        child.release_workspace_write_lock("t")
        child.release_workspace_write_lock("t")
        assert_os_locked(tmp_path)
        assert owned._lock._handle is handle
        await child.acquire_workspace_write_lock("t")
        child.release_workspace_write_lock("t")
    finally:
        owned.close()
        owned.close()
    assert handle[0].closed
    fresh = owner(tmp_path)
    try:
        assert await asyncio.wait_for(fresh.child("new").acquire_workspace_write_lock("t"), 1)
    finally:
        fresh.close()


@pytest.mark.asyncio
async def test_parent_can_wait_for_multiple_child_writes_without_holding_gate(tmp_path):
    owned = owner(tmp_path)
    owned.child("parent")  # Parent is orchestration-only: no acquire while awaiting children.
    active = 0
    peak = 0

    async def write(child_id):
        nonlocal active, peak
        child = owned.child(child_id)
        await child.acquire_workspace_write_lock("same-thread")
        try:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
        finally:
            active -= 1
            child.release_workspace_write_lock("same-thread")

    try:
        await asyncio.wait_for(asyncio.gather(*(write(str(i)) for i in range(4))), 2)
        assert peak == 1
        assert_os_locked(tmp_path)
    finally:
        owned.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["gate", "os"])
async def test_cancel_waiter_does_not_leak_gate_or_handle(tmp_path, stage):
    owned = owner(tmp_path)
    child = owned.child("a")
    external = probe(tmp_path)
    if stage == "gate":
        await child.acquire_workspace_write_lock("t")
    else:
        await external.acquire()
    try:
        waiter = await queued(owned.child("b"), "t")
        assert not waiter.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        child.release_workspace_write_lock("t")
        external.release()
        next_child = owned.child("c")
        assert await asyncio.wait_for(next_child.acquire_workspace_write_lock("t"), 1)
        next_child.release_workspace_write_lock("t")
        assert_os_locked(tmp_path)
    finally:
        external.release()
        owned.close()
    await asyncio.wait_for(external.acquire(), 1)
    external.release()


@pytest.mark.asyncio
async def test_other_owner_and_process_cannot_reenter(tmp_path):
    owned, other = owner(tmp_path), owner(tmp_path / ".")
    child = owned.child("same-child")
    await child.acquire_workspace_write_lock("same-thread")
    child.release_workspace_write_lock("same-thread")
    waiter = await queued(other.child("same-child"), "same-thread")
    try:
        assert not waiter.done()
        code = """
import sys
from pathlib import Path
from voidx.persistence.file_lock import acquire_file_lock_sync, release_file_lock_sync
try:
    handle = acquire_file_lock_sync(Path(sys.argv[1]), blocking=False)
except BlockingIOError:
    sys.exit(23)
release_file_lock_sync(handle)
"""
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, str(probe(tmp_path)._path)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 23, result.stderr
        owned.close()
        assert await asyncio.wait_for(waiter, 1)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        owned.close()
        other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["gate", "os"])
async def test_close_wakes_waiters_and_revokes_capabilities(tmp_path, stage):
    owned = owner(tmp_path)
    child = owned.child("a")
    external = probe(tmp_path)
    if stage == "gate":
        await child.acquire_workspace_write_lock("t")
    else:
        await external.acquire()
    waiter = await queued(owned.child("b"), "t")
    try:
        owned.close()
        with pytest.raises(RuntimeError, match="closed"):
            await asyncio.wait_for(waiter, 1)
        with pytest.raises(RuntimeError, match="closed"):
            await child.acquire_workspace_write_lock("t")
        with pytest.raises(RuntimeError, match="closed"):
            owned.child("new")
        child.release_workspace_write_lock("t")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        external.release()
        owned.close()
    await asyncio.wait_for(external.acquire(), 1)
    external.release()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['failure', 'cancel'])
async def test_real_loop_child_initialization_holds_session_lock_and_releases(tmp_path, monkeypatch, stage):
    from voidx.bootstrap import autonomous_headless, headless
    from voidx.agent.adapters.persistence.session_repository import create_session
    from voidx.agent.application.runtime.autonomous_session import SessionRunCoordinator
    from voidx.agent.application.runtime.contracts import TurnRequest
    from voidx.agent.domain.thread import AgentThread
    from voidx.agent.domain.turn_context import TurnExecutionContext

    root = await create_session(workspace=str(tmp_path), profile='loop')
    child = await create_session(workspace=str(tmp_path), profile='loop')
    root_lock = locks.HeadlessFileLock('session', root.id)
    await root_lock.acquire()
    entered = asyncio.Event()
    blocked = []

    async def initialize(*args, **kwargs):
        probe = locks.HeadlessFileLock('session', child.id)
        try:
            handle = acquire_file_lock_sync(probe._path, blocking=False)
        except BlockingIOError:
            blocked.append(True)
        else:
            release_file_lock_sync(handle)
            blocked.append(False)
        entered.set()
        if stage == 'cancel':
            await asyncio.Event().wait()
        raise ValueError('child initialization failed')

    async def idle(self, profile, prompt, thread):
        await self.runtime.run_turn(TurnRequest(
            thread=AgentThread(thread_id=child.id, session_id=child.id, workspace=str(tmp_path)),
            user_text='child', context=TurnExecutionContext(thread_id=child.id, session_id=child.id),
        ))

    monkeypatch.setattr(headless, '_build_execution', initialize)
    monkeypatch.setattr(SessionRunCoordinator, 'run_idle', idle)
    run, _ = await autonomous_headless.build_sdk_loop_run(
        None, None, None, 'test', root, str(tmp_path), root_lock,
        execution_factory=headless._build_execution, publisher_factory=headless._RunPublisher,
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if stage == 'failure':
            await asyncio.gather(run._driver, return_exceptions=True)
        await run.cancel()
        assert blocked == [True]
        probe = locks.HeadlessFileLock('session', child.id)
        await asyncio.wait_for(probe.acquire(), 1)
        probe.release()
        assert run._interactions == {}
        assert run.owner.producer_count == 0
        assert root_lock._handle is None
        assert run._workspace_lock._closed
    finally:
        await run.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['goal', 'loop'])
@pytest.mark.parametrize('assembled', [False, True])
@pytest.mark.parametrize('errors', [False, True])
@pytest.mark.parametrize('owns_automation', [False, True])
async def test_loop_close_attempts_every_resource_once_preserving_errors(tmp_path, assembled, errors, profile, owns_automation):
    from types import SimpleNamespace
    from voidx.bootstrap.autonomous_headless import _SDKLoopRun
    from voidx.agent.application.runtime.run_ownership import RunOwner

    calls = []
    failures = [ValueError('owner'), RuntimeError('stop'), OSError('workspace'), LookupError('session')]

    async def stop(session_id):
        assert session_id == 'root'
        calls.append('stop')
        if errors:
            raise failures[1]

    workspace = owner(tmp_path)
    session = locks.HeadlessFileLock('session', 'root')
    await workspace.child('turn').acquire_workspace_write_lock('thread')
    await session.acquire()
    close_workspace, release_session = workspace.close, session.release

    def close():
        calls.append('workspace')
        close_workspace()
        if errors:
            raise failures[2]

    def release():
        calls.append('session')
        release_session()
        if errors:
            raise failures[3]

    workspace.close, session.release = close, release
    run = _SDKLoopRun(RunOwner(), 'root', session, workspace)
    run.profile = profile
    run._owns_automation = owns_automation
    should_stop = assembled and owns_automation
    if assembled:
        run.assembly = SimpleNamespace(**{f'{profile}_service': SimpleNamespace(stop=stop)})
    results = await asyncio.gather(run.cancel(), run.cancel(), return_exceptions=True)
    assert calls == [*(['stop'] if should_stop else []), 'workspace', 'session']
    assert run.owner.completion.done()
    assert session._handle is None
    assert workspace._lock._handle is None
    if errors:
        expected = [*([failures[1]] if should_stop else []), *failures[2:]]
        assert isinstance(results[0], BaseExceptionGroup)
        assert list(results[0].exceptions) == expected
        assert results[1] is results[0]
        assert (await run.owner.completion).outcome == "failed"
    else:
        assert results == [None, None]
