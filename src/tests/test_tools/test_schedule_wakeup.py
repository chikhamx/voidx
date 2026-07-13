from __future__ import annotations

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.schedule_wakeup import ScheduleWakeupTool


class FakeLoopManager:
    def __init__(self, active: bool = True, mode: str = "dynamic") -> None:
        self.calls: list[tuple[float | None, bool]] = []
        self._active = active
        self._mode = mode

    def status(self) -> dict | None:
        if not self._active:
            return None
        return {"active": True, "mode": self._mode}

    def schedule_wakeup(self, delay_seconds: float | None = None, *, stop: bool = False) -> None:
        self.calls.append((delay_seconds, stop))


@pytest.mark.asyncio
async def test_schedule_wakeup_requires_loop_manager() -> None:
    tool = ScheduleWakeupTool()
    ctx = ToolContext(workspace="/tmp/workspace")

    result = await tool.execute({"delay_seconds": 60}, ctx)

    assert result.metadata["error"] is True
    assert "No active /loop manager" in result.output


@pytest.mark.asyncio
async def test_schedule_wakeup_schedules_dynamic_loop() -> None:
    manager = FakeLoopManager(mode="dynamic")
    tool = ScheduleWakeupTool()
    ctx = ToolContext(workspace="/tmp/workspace", loop_manager=manager)

    result = await tool.execute({"delay_seconds": 120}, ctx)

    assert manager.calls == [(120.0, False)]
    assert result.metadata["scheduled"] is True
    assert result.metadata["delay_seconds"] == 120.0


@pytest.mark.asyncio
async def test_schedule_wakeup_rejects_delay_outside_bounds() -> None:
    manager = FakeLoopManager(mode="dynamic")
    tool = ScheduleWakeupTool()
    ctx = ToolContext(workspace="/tmp/workspace", loop_manager=manager)

    result = await tool.execute({"delay_seconds": 59}, ctx)

    assert manager.calls == []
    assert result.metadata["error"] is True
    assert "between 60 and 3600" in result.output


@pytest.mark.asyncio
async def test_schedule_wakeup_can_stop_fixed_loop() -> None:
    manager = FakeLoopManager(mode="fixed")
    tool = ScheduleWakeupTool()
    ctx = ToolContext(workspace="/tmp/workspace", loop_manager=manager)

    result = await tool.execute({"stop": True}, ctx)

    assert manager.calls == [(None, True)]
    assert result.metadata["stopped"] is True
