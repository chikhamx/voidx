"""Gateway permission classification: mcp op=call → mcp:{server}:{tool}."""

from voidx.tooling.domain.authorization import PermissionContext
from voidx.tooling.application.authorization import authorize_tool_call
from voidx.tooling.policy.permission.rules import (
    PermissionCapability,
    classify_tool_call,
)


def _classify(args: dict):
    return classify_tool_call({"name": "mcp", "args": args})


def _context(tmp_path, **overrides) -> PermissionContext:
    return PermissionContext(workspace=str(tmp_path), **overrides)


class TestClassification:
    def test_call_pattern_is_mcp_resource(self):
        classified = _classify({"op": "call", "server": "tavily", "tool": "tavily_search"})
        assert classified.pattern == "mcp:tavily:tavily_search"
        assert classified.capability == PermissionCapability.MCP_TOOLS

    def test_call_with_missing_parts_uses_wildcards(self):
        classified = _classify({"op": "call"})
        assert classified.pattern == "mcp:*:*"
        assert classified.capability == PermissionCapability.MCP_TOOLS

    def test_list_is_read_only(self):
        classified = _classify({"op": "list"})
        assert classified.pattern == "list"
        assert classified.capability == PermissionCapability.READ_TOOLS

    def test_load_is_read_only(self):
        classified = _classify({"op": "load", "server": "tavily"})
        assert classified.pattern == "load"
        assert classified.capability == PermissionCapability.READ_TOOLS


class TestAuthorize:
    def test_list_allowed_by_default(self, tmp_path):
        decision = authorize_tool_call({"name": "mcp", "args": {"op": "list"}}, _context(tmp_path))
        assert decision.action == "allow"

    def test_load_allowed_by_default(self, tmp_path):
        decision = authorize_tool_call(
            {"name": "mcp", "args": {"op": "load", "server": "tavily"}}, _context(tmp_path),
        )
        assert decision.action == "allow"

    def test_call_allowed_by_default(self, tmp_path):
        decision = authorize_tool_call(
            {"name": "mcp", "args": {"op": "call", "server": "tavily", "tool": "tavily_search"}},
            _context(tmp_path),
        )
        assert decision.action == "allow"

    def test_session_allow_pattern_matches_gateway_resource(self, tmp_path):
        context = _context(tmp_path, session_allow=frozenset({"mcp@pattern:mcp:tavily:*"}))
        decision = authorize_tool_call(
            {"name": "mcp", "args": {"op": "call", "server": "tavily", "tool": "tavily_search"}},
            context,
        )
        assert decision.action == "allow"

    def test_session_deny_blocks_specific_tool_only(self, tmp_path):
        context = _context(tmp_path, session_deny=frozenset({"mcp@pattern:mcp:tavily:tavily_extract"}))
        denied = authorize_tool_call(
            {"name": "mcp", "args": {"op": "call", "server": "tavily", "tool": "tavily_extract"}},
            context,
        )
        assert denied.action == "deny"

        allowed = authorize_tool_call(
            {"name": "mcp", "args": {"op": "call", "server": "tavily", "tool": "tavily_search"}},
            context,
        )
        assert allowed.action == "allow"

    def test_wildcard_deny_blocks_everything(self, tmp_path):
        context = _context(tmp_path, session_deny=frozenset({"mcp@pattern:mcp:*:*"}))
        decision = authorize_tool_call(
            {"name": "mcp", "args": {"op": "call", "server": "github", "tool": "create_issue"}},
            context,
        )
        assert decision.action == "deny"
