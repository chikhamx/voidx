"""McpArgumentValidator — parse `arguments` and validate against MCP inputSchema."""

from voidx.mcp.validation import validate_mcp_arguments


_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "max_results": {"type": "integer"},
        "mode": {"type": "string", "enum": ["basic", "advanced"]},
        "urls": {"type": "array", "items": {"type": "string"}},
        "options": {
            "type": "object",
            "properties": {"depth": {"type": "integer"}},
            "required": ["depth"],
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


class TestParse:
    def test_dict_passed_through(self):
        result = validate_mcp_arguments({"query": "x"}, _SCHEMA)
        assert result.error is None
        assert result.arguments == {"query": "x"}

    def test_none_becomes_empty_object(self):
        result = validate_mcp_arguments(None, {})
        assert result.error is None
        assert result.arguments == {}

    def test_string_is_rejected(self):
        result = validate_mcp_arguments('{"query": "x"}', _SCHEMA)
        assert result.error is not None
        assert result.error.kind == "not_object"
        assert "got str" in result.error.message

    def test_non_object_input(self):
        result = validate_mcp_arguments(["query"], _SCHEMA)
        assert result.error is not None
        assert result.error.kind == "not_object"


class TestSchemaValidation:
    def test_missing_required_field(self):
        result = validate_mcp_arguments({}, _SCHEMA)
        assert result.error is not None
        assert result.error.kind == "schema"
        assert "query" in result.error.message

    def test_wrong_primitive_type(self):
        result = validate_mcp_arguments({"query": "x", "max_results": "five"}, _SCHEMA)
        assert result.error is not None
        assert result.error.kind == "schema"
        assert "max_results" in result.error.message

    def test_enum_violation(self):
        result = validate_mcp_arguments({"query": "x", "mode": "deep"}, _SCHEMA)
        assert result.error is not None
        assert result.error.kind == "schema"
        assert "mode" in result.error.message

    def test_array_item_type(self):
        result = validate_mcp_arguments({"query": "x", "urls": ["a", 3]}, _SCHEMA)
        assert result.error is not None
        assert result.error.kind == "schema"

    def test_nested_object_required(self):
        result = validate_mcp_arguments({"query": "x", "options": {}}, _SCHEMA)
        assert result.error is not None
        assert result.error.kind == "schema"
        assert "depth" in result.error.message

    def test_additional_properties_rejected(self):
        result = validate_mcp_arguments({"query": "x", "bogus": 1}, _SCHEMA)
        assert result.error is not None
        assert result.error.kind == "schema"
        assert "bogus" in result.error.message

    def test_valid_full_arguments(self):
        raw = {"query": "x", "max_results": 3, "mode": "basic", "urls": ["a"], "options": {"depth": 2}}
        result = validate_mcp_arguments(raw, _SCHEMA)
        assert result.error is None

    def test_empty_schema_accepts_any_object(self):
        result = validate_mcp_arguments({"anything": True}, {})
        assert result.error is None
