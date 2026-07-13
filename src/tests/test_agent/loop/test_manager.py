from __future__ import annotations

import asyncio

import pytest

from voidx.agent.loop.manager import LoopManager
from voidx.agent.loop.prompt_source import PromptSource


class FakeHost:
    def __init__(self) -> None:
        self.turns: list[str] = []

    async def run_synthetic_turn(self, text: str, *, display_text: str | None = None) -> None:
        self.turns.append(text)


@pytest.mark.asyncio
async def test_fixed_loop_fires_after_interval(tmp_path) -> None:
    host = FakeHost()
    idle = asyncio.Event()
    idle.set()
    manager = LoopManager(host, idle_event=idle, workspace=str(tmp_path))

    manager.start(PromptSource.from_raw("tick"), 0.01)
    for _ in range(20):
        if host.turns:
            break
        await asyncio.sleep(0.005)
    await manager.cleanup()

    assert host.turns[:1] == ["tick"]


@pytest.mark.asyncio
async def test_stop_cancels_active_loop_before_fire(tmp_path) -> None:
    host = FakeHost()
    idle = asyncio.Event()
    idle.set()
    manager = LoopManager(host, idle_event=idle, workspace=str(tmp_path))

    manager.start(PromptSource.from_raw("tick"), 10)
    manager.stop()
    await asyncio.sleep(0)

    assert manager.status() is None
    assert host.turns == []


@pytest.mark.asyncio
async def test_dynamic_wakeup_interrupts_default_sleep(tmp_path) -> None:
    host = FakeHost()
    idle = asyncio.Event()
    idle.set()
    manager = LoopManager(host, idle_event=idle, workspace=str(tmp_path), default_interval_seconds=10)

    manager.start(PromptSource.from_raw("tick"), None)
    await asyncio.sleep(0)
    manager.schedule_wakeup(0.01)
    await asyncio.sleep(0.04)
    await manager.cleanup()

    assert host.turns == ["tick"]



class CrashingHost:
    def __init__(self) -> None:
        self.turns: list[str] = []

    async def run_synthetic_turn(self, text: str, *, display_text: str | None = None) -> None:
        self.turns.append(text)
        raise RuntimeError("turn failed")


@pytest.mark.asyncio
async def test_loop_records_last_error_when_turn_raises(tmp_path) -> None:
    host = CrashingHost()
    idle = asyncio.Event()
    idle.set()
    manager = LoopManager(host, idle_event=idle, workspace=str(tmp_path))

    manager.start(PromptSource.from_raw("tick"), 0.01)
    for _ in range(20):
        if host.turns:
            break
        await asyncio.sleep(0.005)

    assert host.turns == ["tick"]
    assert manager._last_error is not None
    assert "turn failed" in manager._last_error


@pytest.mark.asyncio
async def test_cleanup_does_not_reraise_crashed_task_exception(tmp_path) -> None:
    host = CrashingHost()
    idle = asyncio.Event()
    idle.set()
    manager = LoopManager(host, idle_event=idle, workspace=str(tmp_path))

    manager.start(PromptSource.from_raw("tick"), 0.01)
    await asyncio.sleep(0.05)

    await manager.cleanup()


@pytest.mark.asyncio
async def test_status_includes_prompt_summary_and_remaining_seconds(tmp_path) -> None:
    host = FakeHost()
    idle = asyncio.Event()
    idle.set()
    manager = LoopManager(host, idle_event=idle, workspace=str(tmp_path))

    manager.start(PromptSource.from_raw("check the build"), 60)
    await asyncio.sleep(0)
    status = manager.status()

    assert status is not None
    assert "prompt_summary" in status
    assert "check the build" in status["prompt_summary"]
    assert "next_fire_in_seconds" in status
    assert isinstance(status["next_fire_in_seconds"], (int, float))
    assert 0 < status["next_fire_in_seconds"] <= 60

    await manager.cleanup()
