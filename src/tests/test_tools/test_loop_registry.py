from __future__ import annotations

from voidx.tools.registry import ToolRegistry
from voidx.tools.schedule_wakeup import ScheduleWakeupTool


class FakeLoopManager:
    pass


def test_schedule_wakeup_registered_by_default() -> None:
    registry = ToolRegistry()

    assert "schedule_wakeup" in registry.ids()
    assert isinstance(registry.get("schedule_wakeup"), ScheduleWakeupTool)


def test_filtered_copy_preserves_loop_manager() -> None:
    manager = FakeLoopManager()
    registry = ToolRegistry(loop_manager=manager)

    clone = registry.filtered_copy({"schedule_wakeup"})

    assert clone._loop_manager is manager
    assert "schedule_wakeup" in clone.ids()


def test_subagent_blocked_child_tools_includes_schedule_wakeup() -> None:
    from voidx.agent.graph.subagent import _BLOCKED_CHILD_TOOLS

    assert "schedule_wakeup" in _BLOCKED_CHILD_TOOLS


def test_filtered_copy_can_exclude_schedule_wakeup_for_subagents() -> None:
    registry = ToolRegistry()
    all_ids = set(registry.ids())
    blocked_child_tools = {"agent", "clarify", "checkpoint", "schedule_wakeup"}

    clone = registry.filtered_copy(all_ids - blocked_child_tools)

    assert "schedule_wakeup" not in clone.ids()
    assert "agent" not in clone.ids()
