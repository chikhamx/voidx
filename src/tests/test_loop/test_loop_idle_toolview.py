"""Tests for LoopToolView idle phase — read-only + clarify + loop, no write/shell/web/mcp/skill."""

from voidx.agent.domain.automation.loop import LoopToolView, LOOP_IDLE_DIRECTIVE


_ALL_TOOLS = {
    "read", "find", "search", "lsp", "document",
    "websearch", "webfetch", "mcp", "skill",
    "bash", "write", "replace", "manage",
    "clarify", "loop", "workflow", "todo", "agent",
}


def test_idle_phase_allows_readonly_plus_clarify_and_loop():
    view = LoopToolView.default(phase="idle").bind(_ALL_TOOLS)
    assert "read" in view.bound_tool_ids
    assert "find" in view.bound_tool_ids
    assert "search" in view.bound_tool_ids
    assert "lsp" in view.bound_tool_ids
    assert "document" in view.bound_tool_ids
    assert "clarify" in view.bound_tool_ids
    assert "loop" in view.bound_tool_ids


def test_idle_phase_blocks_write_shell_web_mcp_skill():
    view = LoopToolView.default(phase="idle").bind(_ALL_TOOLS)
    assert "bash" not in view.bound_tool_ids
    assert "write" not in view.bound_tool_ids
    assert "replace" not in view.bound_tool_ids
    assert "manage" not in view.bound_tool_ids
    assert "websearch" not in view.bound_tool_ids
    assert "webfetch" not in view.bound_tool_ids
    assert "mcp" not in view.bound_tool_ids
    assert "skill" not in view.bound_tool_ids
    assert "workflow" not in view.bound_tool_ids
    assert "todo" not in view.bound_tool_ids
    assert "agent" not in view.bound_tool_ids


def test_idle_directive_is_nonempty():
    assert LOOP_IDLE_DIRECTIVE.strip()
    assert "loop" in LOOP_IDLE_DIRECTIVE.lower()
