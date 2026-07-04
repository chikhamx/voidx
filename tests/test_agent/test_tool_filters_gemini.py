"""Tests for Gemini-specific tool schema filtering."""

from voidx.agent.tool_filters import strip_gemini_unsupported_schema_keys


def _sample_tool_defs() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "path"},
                        "offset": {
                            "anyOf": [{"type": "integer"}, {"type": "null"}],
                            "default": None,
                        },
                    },
                    "required": ["file_path", "offset"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mcp__test__search",
                "description": "MCP search",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "options": {
                            "type": "object",
                            "properties": {"limit": {"type": "integer"}},
                            "additionalProperties": False,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _has_additional_properties(obj) -> bool:
    """Recursively check if any dict in the structure has additionalProperties."""
    if isinstance(obj, dict):
        if "additionalProperties" in obj:
            return True
        return any(_has_additional_properties(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_additional_properties(item) for item in obj)
    return False


def test_gemini_strips_additional_properties():
    """Gemini protocol should remove all additionalProperties keys."""
    tool_defs = _sample_tool_defs()
    result = strip_gemini_unsupported_schema_keys(tool_defs, "gemini")

    assert len(result) == 2
    assert not _has_additional_properties(result)


def test_non_gemini_keeps_additional_properties():
    """Non-Gemini protocols should keep additionalProperties unchanged."""
    for protocol in ("openai", "anthropic", "deepseek", None):
        tool_defs = _sample_tool_defs()
        result = strip_gemini_unsupported_schema_keys(tool_defs, protocol)

        assert _has_additional_properties(result), f"additionalProperties should be kept for {protocol}"


def test_gemini_preserves_other_fields():
    """Gemini filtering should only remove additionalProperties, not other fields."""
    tool_defs = _sample_tool_defs()
    result = strip_gemini_unsupported_schema_keys(tool_defs, "gemini")

    read_params = result[0]["function"]["parameters"]
    assert read_params["type"] == "object"
    assert "file_path" in read_params["properties"]
    assert "offset" in read_params["properties"]
    assert read_params["required"] == ["file_path", "offset"]
    assert result[0]["function"]["strict"] is True


def test_gemini_handles_nested_additional_properties():
    """Nested additionalProperties in sub-objects should also be removed."""
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": "test",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "config": {
                            "type": "object",
                            "properties": {"key": {"type": "string"}},
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }
    ]
    result = strip_gemini_unsupported_schema_keys(tool_defs, "gemini")
    assert not _has_additional_properties(result)


def test_gemini_does_not_mutate_original():
    """The original tool_defs should not be mutated."""
    tool_defs = _sample_tool_defs()
    strip_gemini_unsupported_schema_keys(tool_defs, "gemini")

    # Original should still have additionalProperties
    assert _has_additional_properties(tool_defs)
