"""Tests for the simplified child-agent tool contract."""

from voidx.tools.agent import AgentInput, normalize_agent_input
from voidx.tools.base import model_to_json_schema


def test_agent_schema_exposes_spawn_only_contract():
    schema = model_to_json_schema(AgentInput)
    assert set(schema["properties"]) == {"mode", "goal", "detail", "scope"}
    assert set(schema["required"]) == {"mode", "goal", "detail", "scope"}
    assert schema["properties"]["scope"].get("nullable") is True or "null" in str(schema["properties"]["scope"])
    assert set(schema["properties"]["mode"]["enum"]) == {"review", "debug", "implement"}


def test_normalize_agent_input_uses_goal_and_mode_route():
    normalized = normalize_agent_input(
        AgentInput(
            mode="implement",
            goal="精简 agent 工具",
            detail="更新实现和测试。",
            scope="src/voidx/tools/agent.py",
        )
    )
    assert normalized.goal_resolution.goal.desc == "精简 agent 工具"
    assert normalized.goal_resolution.plan.join == "tdd"
    assert normalized.goal_resolution.plan.leave == "verify"
    assert normalized.result_contract.schema_name == "implementation_result"
    assert "Scope: src/voidx/tools/agent.py" in normalized.description
    assert "更新实现和测试。" in normalized.description


def test_normalize_agent_input_maps_review_and_debug_routes():
    for mode, join, leave, schema_name in (
        ("review", "review", "review", "review_result"),
        ("debug", "debug", "debug", "debug_result"),
    ):
        normalized = normalize_agent_input(
            AgentInput(
                mode=mode,
                goal=f"{mode} the issue",
                detail="Report evidence and next steps.",
                scope="src/voidx/tools/agent.py",
            )
        )
        assert normalized.goal_resolution.plan.join == join
        assert normalized.goal_resolution.plan.leave == leave
        assert normalized.result_contract.schema_name == schema_name


def test_agent_schema_rejects_invalid_mode():
    try:
        AgentInput(
            mode="inspect",
            goal="Inspect the issue",
            detail="Report findings.",
            scope="src",
        )
    except ValueError:
        return
    raise AssertionError("invalid agent mode must be rejected")
