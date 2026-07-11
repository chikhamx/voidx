from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Any, TypeVar
from weakref import WeakKeyDictionary


_ProcessT = TypeVar("_ProcessT")
_FINALIZER_TASKS: WeakKeyDictionary[Any, asyncio.Task[None]] = WeakKeyDictionary()
_WIN32_JOB_HANDLES: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


def process_launch_options() -> dict[str, Any]:
    if os.name == "nt":
        return {}
    return {"start_new_session": True}


async def create_owned_subprocess_exec(
    program: str,
    *args: str,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    return await create_owned_process(
        lambda: _create_subprocess_exec(program, *args, **kwargs)
    )


async def create_owned_subprocess_shell(
    command: str,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    return await create_owned_process(
        lambda: _create_subprocess_shell(command, **kwargs)
    )


async def _create_subprocess_exec(
    program: str,
    *args: str,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    if os.name != "nt":
        return await asyncio.create_subprocess_exec(
            program,
            *args,
            **kwargs,
            **process_launch_options(),
        )
    return await _create_win32_job_subprocess("exec", [program, *args], kwargs)


async def _create_subprocess_shell(
    command: str,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    if os.name != "nt":
        return await asyncio.create_subprocess_shell(
            command,
            **kwargs,
            **process_launch_options(),
        )
    return await _create_win32_job_subprocess("shell", [command], kwargs)


async def _create_win32_job_subprocess(
    mode: str,
    command: Sequence[str],
    kwargs: dict[str, Any],
) -> asyncio.subprocess.Process:
    win32_jobs = _win32_jobs_module()
    job = win32_jobs.create_kill_on_close_job()
    try:
        create_kwargs = dict(kwargs)
        create_kwargs["env"] = win32_jobs.broker_environment(
            job,
            create_kwargs.get("env"),
        )
        create_kwargs["startupinfo"] = win32_jobs.startupinfo_for_job(job)
        create_kwargs["close_fds"] = True
        proc = await asyncio.create_subprocess_exec(
            *win32_jobs.broker_command(mode, list(command)),
            **create_kwargs,
        )
    except BaseException:
        job.close()
        raise
    job.release_broker_handle()
    _WIN32_JOB_HANDLES[proc] = job
    return proc


def _win32_jobs_module() -> Any:
    from voidx.runtime import _win32_jobs

    return _win32_jobs


async def create_owned_process(
    create: Callable[[], Awaitable[_ProcessT]],
) -> _ProcessT:
    creation_task = asyncio.create_task(create())
    process, cancellation = await _await_task_deferring_cancellation(creation_task)
    if cancellation is None:
        return process

    try:
        await finalize_process_tree(process)
    except asyncio.CancelledError:
        pass
    raise cancellation


async def release_owned_process(process: Any) -> None:
    job = _WIN32_JOB_HANDLES.pop(process, None)
    if job is not None:
        job.close()


async def finalize_process_tree(process: Any) -> None:
    finalizer = _FINALIZER_TASKS.get(process)
    if finalizer is None:
        finalizer = asyncio.create_task(_terminate_process_tree(process))
        _FINALIZER_TASKS[process] = finalizer

    _, cancellation = await _await_task_deferring_cancellation(finalizer)
    if cancellation is not None:
        raise cancellation


async def _await_task_deferring_cancellation(
    task: asyncio.Task[_ProcessT],
) -> tuple[_ProcessT, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
        except BaseException:
            break

    try:
        return task.result(), cancellation
    except BaseException:
        if cancellation is not None:
            raise cancellation
        raise


async def _terminate_process_tree(process: Any) -> None:
    if os.name == "nt":
        await _terminate_win32_process_tree(process)
        return

    if hasattr(os, "killpg"):
        await _terminate_posix_process_group(process)
        return

    if getattr(process, "returncode", None) is not None:
        return

    try:
        process.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        pass

    with suppress(ProcessLookupError):
        process.kill()
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=2)


async def _terminate_posix_process_group(process: Any) -> None:
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return

    parent_wait_task: asyncio.Task[Any] | None = None
    if getattr(process, "returncode", None) is None:
        parent_wait_task = asyncio.create_task(process.wait())
        await asyncio.sleep(0)

    try:
        group_exited = await _wait_for_posix_process_group_exit(
            process_group_id,
            timeout=2.0,
        )
        if not group_exited:
            with suppress(ProcessLookupError):
                os.killpg(process_group_id, signal.SIGKILL)
            await _wait_for_posix_process_group_exit(process_group_id, timeout=2.0)
    finally:
        if parent_wait_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(parent_wait_task), timeout=2)
            except asyncio.TimeoutError:
                parent_wait_task.cancel()
                with suppress(asyncio.CancelledError):
                    await parent_wait_task


async def _wait_for_posix_process_group_exit(
    process_group_id: int,
    *,
    timeout: float,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _posix_process_group_exists(process_group_id):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.05, remaining))
    return True


def _posix_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _terminate_win32_process_tree(process: Any) -> None:
    job = _WIN32_JOB_HANDLES.pop(process, None)
    if job is not None:
        try:
            job.terminate()
        finally:
            job.close()
        if getattr(process, "returncode", None) is None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2)
        return

    if getattr(process, "returncode", None) is not None:
        return

    proc = await asyncio.create_subprocess_exec(
        "taskkill",
        "/T",
        "/F",
        "/PID",
        str(process.pid),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2)
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"taskkill failed with exit code {proc.returncode}")
    if getattr(process, "returncode", None) is None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2)
