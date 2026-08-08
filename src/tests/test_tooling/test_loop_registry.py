from __future__ import annotations

from tests.tool_registry import build_registry
from voidx.tooling.application.registry import ToolRegistry


def test_loop_tool_is_registered_but_not_visible_in_default_llm_tools() -> None:
    registry = build_registry()

    assert "loop" in registry.ids()
    assert registry.get("loop") is not None
    assert "loop" in {tool["function"]["name"] for tool in registry.serialize_definitions()}


def test_schedule_wakeup_not_registered() -> None:
    registry = build_registry()

    assert "schedule_wakeup" not in registry.ids()


def test_subagent_blocked_child_tools_excludes_schedule_wakeup() -> None:
    from voidx.agent.adapters.langgraph.runtime.subagent import _BLOCKED_CHILD_TOOLS

    assert "schedule_wakeup" not in _BLOCKED_CHILD_TOOLS


def test_filtered_copy_can_exclude_interactive_tools_for_subagents() -> None:
    registry = build_registry()
    all_ids = set(registry.ids())
    blocked_child_tools = {"agent", "clarify", "checkpoint"}

    clone = registry.filtered_copy(all_ids - blocked_child_tools)

    assert "clarify" not in clone.ids()
    assert "agent" not in clone.ids()


def test_loop_tool_view_binds_closed_world_tools_with_stable_loop_tool() -> None:
    registry = build_registry()

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


def test_loop_tool_view_filters_real_llm_tool_definitions_via_resolver() -> None:
    from voidx.agent.domain.automation.loop import LoopToolView
    from voidx.agent.domain.profile import RuntimeProfile
    from voidx.agent.adapters.langgraph.runtime.tool_surface import (
        ToolSurfaceContext,
        resolve_tool_surface,
    )

    registry = build_registry()
    tool_view = LoopToolView.default(workflow_enabled=False).bind(registry.ids())
    surface = resolve_tool_surface(
        registry,
        ToolSurfaceContext(
            runtime_profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop", protocol="loop"),
            loop_phase="work",
            tool_policy=tool_view,
        ),
    )
    visible_tool_names = {tool["function"]["name"] for tool in surface.definitions}

    assert "loop" in visible_tool_names
    assert "read" in visible_tool_names
    assert "schedule_wakeup" not in visible_tool_names
    assert "bash" in visible_tool_names
    assert "write" not in visible_tool_names
