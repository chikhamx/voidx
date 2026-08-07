def test_default_tool_output_limit_is_independent_of_agent_package():
    from voidx.tooling.domain.output_policy import DEFAULT_TOOL_OUTPUT_MAX_CHARS

    assert DEFAULT_TOOL_OUTPUT_MAX_CHARS == 4_000
