"""Tests for the simplified child-agent tool contract."""

from voidx.agent.adapters.tools.subagent import AgentInput, AgentTool, normalize_agent_input


def test_agent_schema_exposes_spawn_only_contract():
    schema = AgentTool().parameters_schema()
    assert set(schema["properties"]) == {"mode", "goal", "detail", "scope"}
    assert set(schema["required"]) == {"mode", "goal", "detail", "scope"}
    assert schema["properties"]["scope"]["type"] == "string"
    assert set(schema["properties"]["mode"]["enum"]) == {"review", "debug", "implement"}



def test_agent_input_accepts_omitted_scope():
    inp = AgentInput(mode="review", goal="Review the change", detail="Report findings.")

    assert inp.scope == ""


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
