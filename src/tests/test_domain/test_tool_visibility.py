from tests.tool_registry import build_registry
def test_tool_registry_keeps_lifecycle_tool_definitions_stable() -> None:
    from voidx.tooling.application.registry import ToolRegistry

    registry = build_registry()
    lifecycle_ids = {"goal_init", "goal_checkpoint", "goal_decision", "loop"}
    first = {
        tool.id: (tool.description, tool.parameters)
        for tool in registry.list()
        if tool.id in lifecycle_ids
    }
    second = {
        tool.id: (tool.description, tool.parameters)
        for tool in registry.list()
        if tool.id in lifecycle_ids
    }

    assert set(first) == lifecycle_ids
    assert second == first


def test_goal_evaluator_view_allows_decision_but_not_write_tools() -> None:
    from voidx.agent.domain.automation.goal import GoalToolView

    view = GoalToolView.default(phase="evaluator").bind(
        {"read", "write", "replace", "manage", "lsp_format", "goal_decision"}
    )

    assert view.allows("read") is True
    assert view.allows("goal_decision") is True
    assert view.allows("write") is False
    assert view.allows("replace") is False
    assert view.allows("manage") is False
    assert view.allows("lsp_format") is False
