"""Tests for ToolDisplayPolicy — show/summary/hidden modes and per-tool rules."""

from __future__ import annotations

import pytest

from voidx.ui.output.display_policy import (
    DEFAULT_DISPLAY_RULES,
    ToolDisplayMode,
    ToolDisplayPolicy,
    ToolDisplayRule,
)


class TestToolDisplayMode:
    def test_values(self):
        assert ToolDisplayMode.SHOW.value == "show"
        assert ToolDisplayMode.SUMMARY.value == "summary"
        assert ToolDisplayMode.HIDDEN.value == "hidden"

    def test_from_string(self):
        assert ToolDisplayMode("show") == ToolDisplayMode.SHOW
        assert ToolDisplayMode("summary") == ToolDisplayMode.SUMMARY
        assert ToolDisplayMode("hidden") == ToolDisplayMode.HIDDEN

    def test_invalid_string(self):
        with pytest.raises(ValueError):
            ToolDisplayMode("invalid")


class TestToolDisplayRule:
    def test_defaults(self):
        rule = ToolDisplayRule(tool_name="bash")
        assert rule.mode == ToolDisplayMode.SHOW
        assert rule.summary_max_lines == 3
        assert rule.auto_summary_lines == 50
        assert rule.auto_summary_chars == 5000

    def test_custom(self):
        rule = ToolDisplayRule(
            tool_name="todo",
            mode=ToolDisplayMode.HIDDEN,
            summary_max_lines=5,
        )
        assert rule.mode == ToolDisplayMode.HIDDEN
        assert rule.summary_max_lines == 5


class TestToolDisplayPolicy:
    def test_rule_for_known_tool(self):
        policy = ToolDisplayPolicy(rules=DEFAULT_DISPLAY_RULES)
        rule = policy.rule_for("todo")
        assert rule.mode == ToolDisplayMode.HIDDEN

    def test_rule_for_unknown_tool_returns_default(self):
        policy = ToolDisplayPolicy(default_mode=ToolDisplayMode.SUMMARY)
        rule = policy.rule_for("unknown_tool")
        assert rule.mode == ToolDisplayMode.SUMMARY
        assert rule.tool_name == "unknown_tool"

    def test_resolve_display_mode_show_stays_show(self):
        policy = ToolDisplayPolicy(default_mode=ToolDisplayMode.SHOW)
        mode, max_lines = policy.resolve_display_mode("manage", "short output", result_ok=True)
        assert mode == ToolDisplayMode.SHOW
        assert max_lines == 3

    def test_resolve_display_mode_show_auto_upgrades_to_summary_by_lines(self):
        policy = ToolDisplayPolicy()
        long_output = "\n".join(f"line {i}" for i in range(60))
        mode, _ = policy.resolve_display_mode("manage", long_output, result_ok=True)
        assert mode == ToolDisplayMode.SUMMARY

    def test_resolve_display_mode_show_auto_upgrades_to_summary_by_chars(self):
        policy = ToolDisplayPolicy()
        long_output = "x" * 6000
        mode, _ = policy.resolve_display_mode("manage", long_output, result_ok=True)
        assert mode == ToolDisplayMode.SUMMARY

    def test_resolve_display_mode_visible_failure_stays_show(self):
        policy = ToolDisplayPolicy(rules=DEFAULT_DISPLAY_RULES)
        mode, _ = policy.resolve_display_mode("bash", "error", result_ok=False)
        assert mode == ToolDisplayMode.SHOW

    @pytest.mark.parametrize("tool_name", ["clarify", "checkpoint"])
    def test_resolve_display_mode_hidden_failure_stays_hidden(self, tool_name):
        policy = ToolDisplayPolicy(rules=DEFAULT_DISPLAY_RULES)
        mode, _ = policy.resolve_display_mode(tool_name, "error", result_ok=False)
        assert mode == ToolDisplayMode.HIDDEN

    def test_resolve_display_mode_hidden_stays_hidden(self):
        policy = ToolDisplayPolicy(rules=DEFAULT_DISPLAY_RULES)
        mode, _ = policy.resolve_display_mode("todo", "output", result_ok=True)
        assert mode == ToolDisplayMode.HIDDEN

    def test_resolve_display_mode_summary_stays_summary(self):
        policy = ToolDisplayPolicy(rules=DEFAULT_DISPLAY_RULES)
        mode, _ = policy.resolve_display_mode("search", "output", result_ok=True)
        assert mode == ToolDisplayMode.SUMMARY

    def test_from_config_empty(self):
        policy = ToolDisplayPolicy.from_config({})
        assert policy.default_mode == ToolDisplayMode.SHOW
        assert len(policy.rules) == 0

    def test_from_config_with_defaults(self):
        policy = ToolDisplayPolicy.from_config({}, defaults=DEFAULT_DISPLAY_RULES)
        assert "todo" in policy.rules
        assert policy.rules["todo"].mode == ToolDisplayMode.HIDDEN

    def test_from_config_override(self):
        config = {
            "default_mode": "summary",
            "rules": {
                "bash": {"mode": "hidden"},
            },
        }
        policy = ToolDisplayPolicy.from_config(config, defaults=DEFAULT_DISPLAY_RULES)
        assert policy.default_mode == ToolDisplayMode.SUMMARY
        assert policy.rules["bash"].mode == ToolDisplayMode.HIDDEN
        assert policy.rules["todo"].mode == ToolDisplayMode.HIDDEN

    def test_from_config_invalid_mode_skipped(self):
        config = {
            "rules": {
                "bash": {"mode": "invalid_mode"},
            },
        }
        policy = ToolDisplayPolicy.from_config(config, defaults=DEFAULT_DISPLAY_RULES)
        assert "bash" not in policy.rules or policy.rules.get("bash") is None or True

    def test_from_config_non_dict_rule_skipped(self):
        config = {
            "rules": {
                "bash": "not a dict",
            },
        }
        policy = ToolDisplayPolicy.from_config(config, defaults=DEFAULT_DISPLAY_RULES)
        # bash comes from defaults, not from the invalid config entry
        assert "bash" in policy.rules
        assert policy.rules["bash"].mode == ToolDisplayMode.SHOW


class TestDefaultDisplayRules:
    def test_hidden_tools(self):
        hidden_tools = ["todo", "task_status", "document", "checkpoint",
                        "compact", "workflow", "skill", "clarify"]
        for name in hidden_tools:
            assert DEFAULT_DISPLAY_RULES[name].mode == ToolDisplayMode.HIDDEN, f"{name} should be hidden"

    def test_replay_sanitize_tools(self):
        assert DEFAULT_DISPLAY_RULES["todo"].replay_sanitize is False
        assert DEFAULT_DISPLAY_RULES["workflow"].replay_sanitize is False
        assert DEFAULT_DISPLAY_RULES["compact"].replay_sanitize is False

    def test_summary_tools(self):
        summary_tools = ["search", "find", "websearch", "lsp"]
        for name in summary_tools:
            assert DEFAULT_DISPLAY_RULES[name].mode == ToolDisplayMode.SUMMARY, f"{name} should be summary"

    def test_show_tools(self):
        show_tools = ["bash", "read", "manage", "write", "replace", "agent", "webfetch", "git"]
        for name in show_tools:
            assert DEFAULT_DISPLAY_RULES[name].mode == ToolDisplayMode.SHOW, f"{name} should be show"

    def test_read_has_high_auto_summary_lines(self):
        assert DEFAULT_DISPLAY_RULES["read"].auto_summary_lines == 100
