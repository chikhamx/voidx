from __future__ import annotations

import asyncio
import os
import signal
import subprocess

import pytest

from voidx.runtime import processes


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.pid = 12345


@pytest.mark.asyncio
async def test_create_owned_process_returns_created_process():
    proc = _FakeProcess()

    async def create():
        return proc

    assert await processes.create_owned_process(create) is proc


@pytest.mark.asyncio
async def test_create_owned_process_resolves_ownership_before_propagating_cancellation(monkeypatch):
    proc = _FakeProcess()
    creation_started = asyncio.Event()
    release_creation = asyncio.Event()
    finalized = asyncio.Event()

    async def create():
        creation_started.set()
        await release_creation.wait()
        return proc

    async def finalize(owned):
        assert owned is proc
        await asyncio.sleep(0)
        finalized.set()

    monkeypatch.setattr(processes, "finalize_process_tree", finalize)
    task = asyncio.create_task(processes.create_owned_process(create))
    await creation_started.wait()
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    release_creation.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert finalized.is_set()


@pytest.mark.asyncio
async def test_create_owned_process_propagates_creation_failure_after_cancellation(monkeypatch):
    creation_started = asyncio.Event()
    release_creation = asyncio.Event()
    finalized = False

    async def create():
        creation_started.set()
        await release_creation.wait()
        raise RuntimeError("spawn failed")

    async def finalize(_owned):
        nonlocal finalized
        finalized = True

    monkeypatch.setattr(processes, "finalize_process_tree", finalize)
    task = asyncio.create_task(processes.create_owned_process(create))
    await creation_started.wait()
    task.cancel()
    release_creation.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert finalized is False


@pytest.mark.asyncio
async def test_finalize_process_tree_defers_repeated_cancellation(monkeypatch):
    proc = _FakeProcess()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def terminate(_proc):
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    monkeypatch.setattr(processes, "_terminate_process_tree", terminate)
    task = asyncio.create_task(processes.finalize_process_tree(proc))
    await cleanup_started.wait()
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished.is_set()


def test_process_launch_options_create_tree_terminable_group():
    options = processes.process_launch_options()

    if os.name == "nt":
        assert options == {}
    else:
        assert options == {"start_new_session": True}


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
async def test_finalize_process_tree_cleans_group_after_parent_exits(monkeypatch):
    proc = _FakeProcess()
    proc.returncode = 0
    signals: list[int] = []
    waits: list[float] = []

    def killpg(pid: int, sig: int) -> None:
        assert pid == proc.pid
        signals.append(sig)

    async def wait_for_group(pid: int, *, timeout: float) -> bool:
        assert pid == proc.pid
        waits.append(timeout)
        return True

    monkeypatch.setattr(processes.os, "killpg", killpg)
    monkeypatch.setattr(processes, "_wait_for_posix_process_group_exit", wait_for_group)

    await processes.finalize_process_tree(proc)

    assert signals == [signal.SIGTERM]
    assert waits == [2.0]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
async def test_finalize_process_tree_reaps_parent_while_waiting_for_group(monkeypatch):
    proc = _FakeProcess()
    wait_started = asyncio.Event()
    signals: list[int] = []

    async def wait() -> int:
        wait_started.set()
        proc.returncode = 0
        return 0

    def killpg(pid: int, sig: int) -> None:
        assert pid == proc.pid
        signals.append(sig)

    async def wait_for_group(pid: int, *, timeout: float) -> bool:
        assert pid == proc.pid
        assert wait_started.is_set()
        return True

    proc.wait = wait
    monkeypatch.setattr(processes.os, "killpg", killpg)
    monkeypatch.setattr(processes, "_wait_for_posix_process_group_exit", wait_for_group)

    await processes.finalize_process_tree(proc)

    assert signals == [signal.SIGTERM]
    assert proc.returncode == 0


@pytest.mark.asyncio
async def test_create_owned_subprocess_exec_uses_win32_job_broker(monkeypatch):
    monkeypatch.setattr(processes.os, "name", "nt", raising=False)
    captured: dict[str, object] = {}

    class FakeJob:
        handle = 9876
        broker_handle = None
        released = False

        def release_broker_handle(self):
            self.released = True

    job = FakeJob()

    class FakeWin32Jobs:
        @staticmethod
        def create_kill_on_close_job():
            return job

        @staticmethod
        def broker_command(mode: str, command: list[str]) -> list[str]:
            return ["broker", mode, *command]

        @staticmethod
        def broker_environment(owned_job, env):
            assert owned_job is job
            owned_job.broker_handle = 4321
            return {**(env or {}), "VOIDX_WIN32_JOB_HANDLE": str(owned_job.broker_handle)}

        @staticmethod
        def startupinfo_for_job(owned_job):
            assert owned_job is job
            assert owned_job.broker_handle == 4321
            return "startupinfo"

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(processes, "_win32_jobs_module", lambda: FakeWin32Jobs)
    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    proc = await processes.create_owned_subprocess_exec(
        "python",
        "server.py",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        env={"A": "B"},
    )

    assert captured["args"] == ("broker", "exec", "python", "server.py")
    assert captured["kwargs"] == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "env": {"A": "B", "VOIDX_WIN32_JOB_HANDLE": "4321"},
        "startupinfo": "startupinfo",
        "close_fds": True,
    }
    assert processes._WIN32_JOB_HANDLES[proc] is job
    assert job.released is True


@pytest.mark.asyncio
async def test_finalize_process_tree_terminates_win32_job_and_waits_parent(monkeypatch):
    monkeypatch.setattr(processes.os, "name", "nt", raising=False)
    proc = _FakeProcess()
    wait_finished = asyncio.Event()

    async def wait() -> int:
        wait_finished.set()
        proc.returncode = 0
        return 0

    class FakeJob:
        def __init__(self) -> None:
            self.terminated = False
            self.closed = False

        def terminate(self) -> None:
            self.terminated = True

        def close(self) -> None:
            self.closed = True

    job = FakeJob()
    proc.wait = wait
    processes._WIN32_JOB_HANDLES[proc] = job

    await processes.finalize_process_tree(proc)

    assert job.terminated is True
    assert job.closed is True
    assert wait_finished.is_set()
    assert proc not in processes._WIN32_JOB_HANDLES


@pytest.mark.asyncio
async def test_finalize_process_tree_raises_when_win32_taskkill_fails_without_job(monkeypatch):
    monkeypatch.setattr(processes.os, "name", "nt", raising=False)
    proc = _FakeProcess()
    captured: dict[str, object] = {}

    class FakeTaskkill:
        returncode = 1

        async def wait(self) -> int:
            return self.returncode

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeTaskkill()

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="taskkill failed"):
        await processes.finalize_process_tree(proc)

    assert captured["args"] == ("taskkill", "/T", "/F", "/PID", str(proc.pid))
    assert captured["kwargs"] == {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }


@pytest.mark.asyncio
async def test_finalize_process_tree_kills_timed_out_win32_taskkill(monkeypatch):
    monkeypatch.setattr(processes.os, "name", "nt", raising=False)
    proc = _FakeProcess()

    class FakeTaskkill:
        returncode = None

        def __init__(self) -> None:
            self.killed = False
            self.waits = 0

        async def wait(self) -> int:
            self.waits += 1
            raise asyncio.TimeoutError

        def kill(self) -> None:
            self.killed = True

    taskkill = FakeTaskkill()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return taskkill

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(asyncio.TimeoutError):
        await processes.finalize_process_tree(proc)

    assert taskkill.killed is True
    assert taskkill.waits == 2


@pytest.mark.asyncio
async def test_release_owned_process_closes_win32_job_without_terminating(monkeypatch):
    monkeypatch.setattr(processes.os, "name", "nt", raising=False)
    proc = _FakeProcess()

    class FakeJob:
        def __init__(self) -> None:
            self.terminated = False
            self.closed = False

        def terminate(self) -> None:
            self.terminated = True

        def close(self) -> None:
            self.closed = True

    job = FakeJob()
    processes._WIN32_JOB_HANDLES[proc] = job

    await processes.release_owned_process(proc)

    assert job.terminated is False
    assert job.closed is True
    assert proc not in processes._WIN32_JOB_HANDLES
