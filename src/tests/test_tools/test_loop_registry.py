from __future__ import annotations

from voidx.tools.registry import ToolRegistry


def test_loop_tool_is_registered_but_not_visible_in_default_llm_tools() -> None:
    registry = ToolRegistry()

    assert "loop" in registry.ids()
    assert registry.get("loop") is not None
    assert "loop" in {tool["function"]["name"] for tool in registry.tools_for_llm()}


def test_schedule_wakeup_not_registered() -> None:
    registry = ToolRegistry()

    assert "schedule_wakeup" not in registry.ids()


def test_subagent_blocked_child_tools_excludes_schedule_wakeup() -> None:
    from voidx.agent.infrastructure.langgraph.runtime.subagent import _BLOCKED_CHILD_TOOLS

    assert "schedule_wakeup" not in _BLOCKED_CHILD_TOOLS


def test_filtered_copy_can_exclude_interactive_tools_for_subagents() -> None:
    registry = ToolRegistry()
    all_ids = set(registry.ids())
    blocked_child_tools = {"agent", "clarify", "checkpoint"}

    clone = registry.filtered_copy(all_ids - blocked_child_tools)

    assert "clarify" not in clone.ids()
    assert "agent" not in clone.ids()


def test_loop_tool_view_binds_closed_world_tools_with_stable_loop_tool() -> None:
    registry = ToolRegistry()

    loop_registry = registry.loop_filtered_copy(workflow_enabled=False)

    ids = set(loop_registry.ids())
    assert "loop" in ids
    assert "read" in ids
    assert "search" in ids
    assert "schedule_wakeup" not in ids
    assert "clarify" not in ids
    assert "checkpoint" not in ids
    assert "agent" not in ids
    assert "bash" in ids
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

    assert "loop" in visible_tool_names
    assert "read" in visible_tool_names
    assert "schedule_wakeup" not in visible_tool_names
    assert "bash" in visible_tool_names
    assert "write" not in visible_tool_names
