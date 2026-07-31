from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.infrastructure.langgraph.runtime.llm_turn import filter_profile_tool_definitions


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


def test_goal_tool_is_hidden_from_coding_and_loop_profiles() -> None:
    definitions = [_tool("read"), _tool("goal"), _tool("loop")]

    coding = filter_profile_tool_definitions(
        definitions, RuntimeProfile(profile_id="coding", revision=1, name="Coding")
    )
    loop = filter_profile_tool_definitions(
        definitions, RuntimeProfile(profile_id="loop", revision=1, name="Loop", protocol="loop")
    )

    assert [item["function"]["name"] for item in coding] == ["read"]
    assert [item["function"]["name"] for item in loop] == ["read", "loop"]


def test_goal_tool_is_visible_to_goal_profile() -> None:
    definitions = [_tool("read"), _tool("goal")]

    filtered = filter_profile_tool_definitions(
        definitions, RuntimeProfile(profile_id="goal", revision=1, name="Goal", protocol="goal")
    )

    assert [item["function"]["name"] for item in filtered] == ["read", "goal"]


def test_coding_and_chat_hide_both_lifecycle_tools() -> None:
    definitions = [_tool("read"), _tool("goal"), _tool("loop")]

    for profile_id in ("coding", "chat", "plan"):
        filtered = filter_profile_tool_definitions(
            definitions, RuntimeProfile(profile_id=profile_id, revision=1, name=profile_id.title())
        )
        assert [item["function"]["name"] for item in filtered] == ["read"]


def test_loop_profile_exposes_loop_but_not_goal() -> None:
    definitions = [_tool("read"), _tool("goal"), _tool("loop")]

    filtered = filter_profile_tool_definitions(
        definitions, RuntimeProfile(profile_id="loop", revision=1, name="Loop", protocol="loop")
    )

    assert [item["function"]["name"] for item in filtered] == ["read", "loop"]


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
