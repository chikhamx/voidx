"""Tests for web/desktop per-thread run manager routing."""
from __future__ import annotations

import asyncio

import pytest

from voidx.presentation.gateway.run_manager import ERR_CONCURRENCY_LIMIT, ThreadRunManager
from voidx.presentation.protocol.v2.methods import MethodParamsError


@pytest.mark.asyncio
async def test_thread_run_manager_rejects_same_thread_double_submit():
    handled = []

    async def command_handler(command):
        handled.append(command)

    manager = ThreadRunManager(command_handler=command_handler, max_concurrent_sessions=2)

    await manager.submit("t1", "first")

    with pytest.raises(MethodParamsError) as exc:
        await manager.submit("t1", "second")

    assert exc.value.code == -32001
    assert [cmd.text for cmd in handled] == ["first"]
    assert manager.status("t1") == "running"


@pytest.mark.asyncio
async def test_thread_run_manager_enforces_concurrency_limit():
    handled = []

    async def command_handler(command):
        handled.append(command)

    manager = ThreadRunManager(command_handler=command_handler, max_concurrent_sessions=2)

    await manager.submit("t1", "first")
    await manager.submit("t2", "second")

    with pytest.raises(MethodParamsError) as exc:
        await manager.submit("t3", "third")

    assert exc.value.code == ERR_CONCURRENCY_LIMIT
    assert [cmd.thread_id for cmd in handled] == ["t1", "t2"]
    assert manager.active_thread_ids() == ["t1", "t2"]


@pytest.mark.asyncio
async def test_thread_run_manager_cancel_targets_only_requested_thread():
    handled = []

    async def command_handler(command):
        handled.append(command)

    manager = ThreadRunManager(command_handler=command_handler, max_concurrent_sessions=2)
    await manager.submit("t1", "first")
    await manager.submit("t2", "second")

    await manager.cancel("t2")

    assert [cmd.kind for cmd in handled] == ["submit", "submit", "cancel"]
    assert handled[-1].thread_id == "t2"
    assert manager.status("t1") == "running"
    assert manager.status("t2") == "cancelling"


def test_thread_run_manager_active_thread_ids_are_sorted():
    manager = ThreadRunManager(command_handler=lambda command: None, max_concurrent_sessions=3)
    manager.mark_running("b")
    manager.mark_running("a")

    assert manager.active_thread_ids() == ["a", "b"]


@pytest.mark.asyncio
async def test_thread_actor_uses_bounded_mailbox_for_submit_cancel():
    handled = []

    async def command_handler(command):
        handled.append(command)

    manager = ThreadRunManager(command_handler=command_handler, max_concurrent_sessions=2)
    actor = manager.actor("t1")

    assert actor.mailbox.maxsize == 2

    await manager.submit("t1", "first")
    await manager.cancel("t1")

    assert [cmd.kind for cmd in handled] == ["submit", "cancel"]
    assert [cmd.thread_id for cmd in handled] == ["t1", "t1"]


@pytest.mark.asyncio
async def test_thread_run_manager_completes_turn_and_releases_concurrency_slot():
    handled = []

    async def command_handler(command):
        handled.append(command)

    manager = ThreadRunManager(command_handler=command_handler, max_concurrent_sessions=1)

    await manager.submit("t1", "first")
    with pytest.raises(MethodParamsError):
        await manager.submit("t2", "blocked")

    manager.complete_turn("t1")
    await manager.submit("t2", "second")

    assert [cmd.thread_id for cmd in handled] == ["t1", "t2"]
    assert manager.status("t1") == "idle"
    assert manager.status("t2") == "running"


@pytest.mark.asyncio
async def test_thread_run_manager_workspace_write_lock_is_fifo():
    manager = ThreadRunManager(command_handler=lambda command: None, max_concurrent_sessions=2)

    first = await manager.acquire_workspace_write_lock("t1")
    second = asyncio.create_task(manager.acquire_workspace_write_lock("t2"))
    await asyncio.sleep(0)

    assert first is True
    assert second.done() is False
    assert manager.status("t1") == "running"
    assert manager.status("t2") == "waiting_for_write_lock"

    manager.release_workspace_write_lock("t1")

    assert await second is True
    assert manager.status("t2") == "running"


@pytest.mark.asyncio
async def test_thread_run_manager_cancel_waiting_write_lock_removes_waiter():
    manager = ThreadRunManager(command_handler=lambda command: None, max_concurrent_sessions=2)

    await manager.acquire_workspace_write_lock("t1")
    waiting = asyncio.create_task(manager.acquire_workspace_write_lock("t2"))
    await asyncio.sleep(0)

    await manager.cancel("t2")
    manager.release_workspace_write_lock("t1")
    await asyncio.sleep(0)

    assert waiting.cancelled()
    assert manager.status("t2") == "cancelling"
    assert manager.workspace_write_lock_holder() == ""


@pytest.mark.asyncio
async def test_thread_run_manager_fail_turn_releases_write_lock_and_promotes_waiter():
    manager = ThreadRunManager(command_handler=lambda command: None, max_concurrent_sessions=2)

    await manager.acquire_workspace_write_lock("t1")
    waiting = asyncio.create_task(manager.acquire_workspace_write_lock("t2"))
    await asyncio.sleep(0)

    manager.fail_turn("t1", "boom")

    assert await waiting is True
    assert manager.workspace_write_lock_holder() == "t2"
    assert manager.status("t1") == "failed"
    assert manager.status("t2") == "running"


@pytest.mark.asyncio
async def test_thread_run_manager_complete_turn_releases_write_lock_and_promotes_waiter():
    manager = ThreadRunManager(command_handler=lambda command: None, max_concurrent_sessions=2)

    await manager.acquire_workspace_write_lock("t1")
    waiting = asyncio.create_task(manager.acquire_workspace_write_lock("t2"))
    await asyncio.sleep(0)

    manager.complete_turn("t1")

    assert await waiting is True
    assert manager.workspace_write_lock_holder() == "t2"
    assert manager.status("t1") == "idle"
    assert manager.status("t2") == "running"
