from voidx.agent.adapters.langgraph.runtime.control_protocol import GoalProtocol


def _names(phase: str) -> list[str]:
    return [item["function"]["name"] for item in GoalProtocol(phase=phase).tool_definitions()]


def test_goal_protocol_exposes_only_the_phase_specific_tool() -> None:
    assert _names("intake") == ["goal_init"]
    assert _names("idle") == ["goal_init"]
    assert _names("work") == ["goal_checkpoint"]
    assert _names("evaluator") == ["goal_decision"]


def test_goal_protocol_does_not_expose_legacy_goal_tool() -> None:
    names = set(_names("evaluator"))
    assert "goal" not in names
    assert "goal_init" not in names
    assert "goal_checkpoint" not in names


def test_goal_init_schema_contains_only_intake_fields() -> None:
    definition = GoalProtocol(phase="intake").tool_definitions()[0]["function"]
    parameters = definition["parameters"]

    assert parameters["required"] == ["objective", "acceptance_condition"]
    assert set(parameters["properties"]) == {
        "objective",
        "acceptance_condition",
        "achievement_method",
        "max_attempts",
    }


def test_goal_checkpoint_schema_contains_typed_evidence_fields() -> None:
    definition = GoalProtocol(phase="work").tool_definitions()[0]["function"]
    properties = definition["parameters"]["properties"]

    assert set(properties) == {
        "summary",
        "evidence",
        "changed_files",
        "verification",
        "next_hint",
        "progress",
    }
    assert properties["progress"]["enum"] == ["none", "partial", "meaningful"]


def test_goal_decision_schema_contains_lifecycle_fields() -> None:
    definition = GoalProtocol(phase="evaluator").tool_definitions()[0]["function"]
    properties = definition["parameters"]["properties"]

    assert properties["status"]["enum"] == ["finished", "continue", "blocked"]
    assert set(properties) == {
        "status",
        "summary",
        "evidence",
        "reason",
        "next_hint",
        "missing_evidence",
        "progress",
    }


def test_goal_protocol_keeps_policy_approved_verification_tools_for_evaluator() -> None:
    protocol = GoalProtocol(phase="evaluator", verification_tool_ids={"mcp", "read"})
    names = {item["function"]["name"] for item in protocol.tool_definitions()}
    assert names == {"goal_decision", "mcp", "read"}
