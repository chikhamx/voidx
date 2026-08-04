def test_tool_registry_keeps_lifecycle_tool_definitions_stable() -> None:
    from voidx.tools.registry import ToolRegistry

    registry = ToolRegistry()
    first = {tool.id: (tool.description, tool.parameters) for tool in registry.list() if tool.id in {"goal", "loop"}}
    second = {tool.id: (tool.description, tool.parameters) for tool in registry.list() if tool.id in {"goal", "loop"}}

    assert set(first) == {"goal", "loop"}
    assert second == first


def test_goal_evaluator_view_does_not_allow_goal_or_write_tools() -> None:
    from voidx.agent.domain.goal import GoalToolView

    view = GoalToolView.default(phase="evaluator").bind({"read", "write", "replace", "manage", "lsp_format", "goal"})

    assert view.allows("read") is True
    assert view.allows("goal") is True
    assert view.allows("write") is False
    assert view.allows("replace") is False
    assert view.allows("manage") is False
    assert view.allows("lsp_format") is False
