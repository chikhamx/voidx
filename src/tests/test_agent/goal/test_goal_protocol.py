from voidx.agent.infrastructure.langgraph.runtime.control_protocol import GoalProtocol


def test_goal_protocol_exposes_goal_control_schema() -> None:
    definitions = GoalProtocol().tool_definitions()
    assert [item["function"]["name"] for item in definitions] == ["goal"]
    parameters = definitions[0]["function"]["parameters"]
    assert parameters["properties"]["op"]["enum"] == ["init", "decision"]
    assert parameters["properties"]["status"]["enum"] == [
        "finished",
        "continue",
        "blocked",
        "",
    ]


def test_goal_protocol_allows_policy_approved_verification_tools() -> None:
    protocol = GoalProtocol(verification_tool_ids={"mcp", "read"})
    names = {item["function"]["name"] for item in protocol.tool_definitions()}
    assert names == {"goal", "mcp", "read"}
