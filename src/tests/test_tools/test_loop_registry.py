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
    from voidx.agent.infrastructure.langgraph.runtime.subagent import _BLOCKED_CHILD_TOOLS

    assert "schedule_wakeup" in _BLOCKED_CHILD_TOOLS


def test_filtered_copy_can_exclude_schedule_wakeup_for_subagents() -> None:
    registry = ToolRegistry()
    all_ids = set(registry.ids())
    blocked_child_tools = {"agent", "clarify", "checkpoint", "schedule_wakeup"}

    clone = registry.filtered_copy(all_ids - blocked_child_tools)

    assert "schedule_wakeup" not in clone.ids()
    assert "agent" not in clone.ids()


def test_loop_tool_view_binds_loop_update_without_legacy_or_interactive_tools() -> None:
    registry = ToolRegistry()

    loop_registry = registry.loop_filtered_copy(workflow_enabled=False)

    ids = set(loop_registry.ids())
    assert "loop_update" in ids
    assert "read" in ids
    assert "search" in ids
    assert "schedule_wakeup" not in ids
    assert "clarify" not in ids
    assert "checkpoint" not in ids
    assert "agent" not in ids
    assert "bash" not in ids
    assert "write" not in ids


def test_loop_tool_view_filters_real_llm_tool_definitions_by_function_name() -> None:
    from voidx.agent.domain.loop import LoopToolView
    from voidx.agent.infrastructure.langgraph.execution import _tool_definition_name

    registry = ToolRegistry()
    tool_view = LoopToolView.default(workflow_enabled=False).bind(registry.ids())
    tool_defs = registry.tools_for_llm()

    visible_tool_names = {
        name
        for tool in tool_defs
        if (name := _tool_definition_name(tool)) and tool_view.allows(name)
    }

    assert "loop_update" in visible_tool_names
    assert "read" in visible_tool_names
    assert "schedule_wakeup" not in visible_tool_names
    assert "bash" not in visible_tool_names
    assert "write" not in visible_tool_names
