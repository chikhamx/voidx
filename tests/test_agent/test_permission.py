"""Tests for permission system — wildcard, evaluate, merge, from_config."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.permission.wildcard import match
from voidx.permission.evaluate import evaluate, from_config, merge
from voidx.permission.schema import Rule, Ruleset
from voidx.permission.engine import (
    PermissionCapability,
    PermissionContext,
    authorize_tool_call,
    classify_tool_call,
)
from voidx.permission.service import PermissionService
from voidx.permission.sandbox import check_sandbox_bash


class TestWildcard:
    def test_exact_match(self):
        assert match("bash", "bash")
        assert not match("read", "edit")

    def test_star_match(self):
        assert match("bash", "*")
        assert match("anything_here", "*")

    def test_path_match(self):
        assert match(".env", "*.env")
        assert match("prod.env", "*.env")
        assert not match(".env.example", "*.env")

    def test_git_commands(self):
        assert match("git push origin main", "git *")
        assert match("git status", "git *")
        assert not match("ls -la", "git *")

    def test_question_mark(self):
        assert match("abc", "a?c")
        assert not match("ac", "a?c")

    def test_windows_path(self):
        assert match("src\\foo\\bar.py", "src/*/*.py")


class TestEvaluate:
    def test_simple_allow(self):
        rules = [Rule(permission="*", pattern="*", action="allow")]
        result = evaluate("bash", "ls", rules)
        assert result.action == "allow"

    def test_specific_override(self):
        rules = [
            Rule(permission="*", pattern="*", action="allow"),
            Rule(permission="bash", pattern="rm *", action="deny"),
        ]
        result = evaluate("bash", "rm -rf /", rules)
        assert result.action == "deny"

    def test_find_last_wins(self):
        rules = [
            Rule(permission="bash", pattern="*", action="allow"),
            Rule(permission="bash", pattern="*", action="deny"),
        ]
        result = evaluate("bash", "anything", rules)
        assert result.action == "deny"

    def test_default_ask(self):
        result = evaluate("unknown", "*")
        assert result.action == "ask"

    def test_multiple_rulesets(self):
        defaults = [Rule(permission="*", pattern="*", action="allow")]
        overrides = [Rule(permission="write", pattern="*.env", action="ask")]
        result = evaluate("write", ".env", defaults, overrides)
        assert result.action == "ask"


class TestFromConfig:
    def test_simple(self):
        ruleset = from_config({"*": "allow"})
        assert len(ruleset) == 1
        assert ruleset[0].permission == "*"
        assert ruleset[0].action == "allow"

    def test_nested(self):
        ruleset = from_config({"read": {"*.env": "ask", "*": "allow"}})
        assert len(ruleset) == 2

    def test_deny_tool(self):
        ruleset = from_config({"write": "deny", "edit": "deny"})
        assert evaluate("write", "foo.py", ruleset).action == "deny"
        assert evaluate("edit", "bar.py", ruleset).action == "deny"

    def test_explore_ruleset(self):
        ruleset = from_config({
            "*": "deny",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
        })
        assert evaluate("read", "x.py", ruleset).action == "allow"
        assert evaluate("write", "x.py", ruleset).action == "deny"
        assert evaluate("bash", "ls", ruleset).action == "deny"


class TestMerge:
    def test_merge_overrides(self):
        a = from_config({"*": "allow"})
        b = from_config({"bash": "deny"})
        merged = merge(a, b)
        assert evaluate("bash", "ls", merged).action == "deny"

    def test_agent_override(self):
        defaults = from_config({"*": "allow"})
        agent = from_config({"write": "deny", "edit": "deny"})
        merged = merge(defaults, agent)
        assert evaluate("read", "x.py", merged).action == "allow"
        assert evaluate("write", "y.py", merged).action == "deny"


def test_permission_service_status_label_ignores_session_overrides():
    service = PermissionService(sandbox_mode="workspace-write")

    assert service.status_label() == "Default"

    service.allow_silent("bash")
    service.deny_silent("write")

    assert service.status_label() == "Default"


def test_permission_service_splits_readonly_and_implement_agents():
    service = PermissionService()

    assert service.decide("agent", "explore") == "allow"
    assert service.decide("agent", "implement") == "ask"


def test_on_intent_is_allowed_runtime_tool(tmp_path):
    context = PermissionContext(workspace=str(tmp_path))
    decision = authorize_tool_call(
        {
            "name": "on_intent",
            "args": {
                "intent": "inspect",
                "confidence": 0.8,
                "reason": "needs workspace context",
            },
        },
        context,
    )

    assert decision.action == "allow"


@pytest.mark.parametrize("tool_name", ["clarify", "plan_checkpoint"])
def test_interactive_runtime_tools_are_allowed(tmp_path, tool_name):
    context = PermissionContext(workspace=str(tmp_path))
    decision = authorize_tool_call(
        {"name": tool_name, "args": {}},
        context,
    )

    assert decision.action == "allow"


def test_permission_service_session_wildcards_apply_to_mcp_tools():
    service = PermissionService()
    tool = "mcp__web-reader__read_url_12345678"

    assert service.decide(tool) == "ask"

    service.allow_silent("mcp__web-reader__*")
    assert service.decide(tool) == "allow"

    service.deny_silent("mcp/web-reader/*")
    assert service.decide(tool) == "deny"


def test_permission_service_allows_read_only_lsp_tools_but_asks_for_format():
    service = PermissionService()

    assert service.decide("lsp_diagnostics") == "allow"
    assert service.decide("lsp_symbols") == "allow"
    assert service.decide("lsp_definition") == "allow"
    assert service.decide("lsp_references") == "allow"
    assert service.decide("lsp_format", "src/app.py") == "ask"


def test_permission_service_mode_presets_update_sandbox_and_approval():
    service = PermissionService()

    service.set_permission_mode("auto-review")
    assert service.permission_mode == "auto-review"
    assert service.sandbox_mode == "workspace-write"
    assert service.approval_policy == "untrusted"
    assert service.approval_reviewer == "auto_review"
    assert service.status_label() == "Auto review"

    service.set_permission_mode("read-only")
    assert service.sandbox_mode == "read-only"
    assert service.approval_policy == "untrusted"
    assert service.approval_reviewer == "user"

    service.set_permission_mode("accept-edits")
    assert service.sandbox_mode == "workspace-write"
    assert service.decide("edit", "src/app.py") == "allow"
    assert service.decide("bash", "python -m pytest") == "ask"

    service.set_permission_mode("full-access")
    assert service.sandbox_mode == "danger-full-access"
    assert service.approval_policy == "never"
    assert service.decide("bash", "python -m pytest") == "allow"
    assert service.status_label() == "Full access"


def test_permission_engine_classifies_basic_capabilities():
    assert classify_tool_call({"name": "read", "args": {"file_path": "x.py"}}).capability == PermissionCapability.READ_TOOLS
    assert classify_tool_call({"name": "edit", "args": {"file_path": "x.py"}}).capability == PermissionCapability.FILE_WRITE
    assert classify_tool_call({"name": "apply_patch", "args": {"patch": ""}}).capability == PermissionCapability.FILE_WRITE
    assert classify_tool_call({"name": "bash", "args": {"command": "ls"}}).capability == PermissionCapability.BASH_READ
    assert classify_tool_call({"name": "bash", "args": {"command": "ls | sort | head"}}).capability == PermissionCapability.BASH_READ
    assert classify_tool_call({"name": "bash", "args": {"command": "python -m pytest"}}).capability == PermissionCapability.BASH_WRITE
    assert classify_tool_call({"name": "bash", "args": {"command": "echo hi>out.txt"}}).capability == PermissionCapability.BASH_WRITE
    assert classify_tool_call({"name": "bash", "args": {"command": "grep foo a.txt | xargs rm"}}).capability == PermissionCapability.BASH_WRITE
    assert classify_tool_call({"name": "bash", "args": {"command": "find . -delete"}}).capability == PermissionCapability.BASH_WRITE
    assert classify_tool_call({"name": "bash", "args": {"command": "git branch new-feature"}}).capability == PermissionCapability.BASH_WRITE
    assert classify_tool_call({"name": "agent", "args": {"agent": "explore"}}).capability == PermissionCapability.AGENT_READONLY
    assert classify_tool_call({"name": "agent", "args": {"agent": "implement"}}).capability == PermissionCapability.AGENT_IMPLEMENT


def test_permission_engine_default_strategy_and_plan_overlay(tmp_path):
    context = PermissionContext(workspace=str(tmp_path))

    assert authorize_tool_call({"name": "read", "args": {"file_path": "x.py"}}, context).action == "allow"
    assert authorize_tool_call({"name": "bash", "args": {"command": "ls"}}, context).action == "allow"
    assert authorize_tool_call({"name": "edit", "args": {"file_path": "x.py"}}, context).action == "ask"
    assert authorize_tool_call({"name": "agent", "args": {"agent": "implement"}}, context).action == "ask"

    plan = PermissionContext(workspace=str(tmp_path), interaction_mode="plan")
    safe_bash = authorize_tool_call({"name": "bash", "args": {"command": "ls"}}, plan)
    unsafe_bash = authorize_tool_call({"name": "bash", "args": {"command": "python -m pytest"}}, plan)
    edit = authorize_tool_call({"name": "edit", "args": {"file_path": "x.py"}}, plan)
    implement = authorize_tool_call({"name": "agent", "args": {"agent": "implement"}}, plan)

    assert safe_bash.action == "allow"
    assert unsafe_bash.action == "deny"
    assert edit.action == "deny"
    assert implement.action == "deny"


def test_permission_engine_policy_presets(tmp_path):
    accept_edits = PermissionContext(
        workspace=str(tmp_path),
        permission_mode="accept-edits",
    )
    full_access = PermissionContext(
        workspace=str(tmp_path),
        sandbox_mode="danger-full-access",
        approval_policy="never",
    )
    on_failure = PermissionContext(
        workspace=str(tmp_path),
        approval_policy="on-failure",
    )

    assert authorize_tool_call({"name": "edit", "args": {"file_path": "x.py"}}, accept_edits).action == "allow"
    assert authorize_tool_call({"name": "bash", "args": {"command": "python -m pytest"}}, accept_edits).action == "ask"
    assert authorize_tool_call({"name": "bash", "args": {"command": "python -m pytest"}}, full_access).action == "allow"

    edit = authorize_tool_call({"name": "edit", "args": {"file_path": "x.py"}}, on_failure)
    bash = authorize_tool_call({"name": "bash", "args": {"command": "python -m pytest"}}, on_failure)

    assert edit.action == "allow"
    assert edit.failure_check is True
    assert bash.action == "ask"


def test_permission_engine_read_only_sandbox_allows_read_bash_but_blocks_writes(tmp_path):
    context = PermissionContext(workspace=str(tmp_path), sandbox_mode="read-only")

    assert authorize_tool_call({"name": "bash", "args": {"command": "ls"}}, context).action == "allow"
    assert authorize_tool_call({"name": "bash", "args": {"command": "python -m pytest"}}, context).action == "deny"
    assert authorize_tool_call({"name": "edit", "args": {"file_path": "x.py"}}, context).action == "deny"


def test_sandbox_bash_tracks_cd_before_relative_write(tmp_path):
    workspace = str(tmp_path / "workspace")
    outside = tmp_path / "outside"
    Path(workspace).mkdir()
    outside.mkdir()

    reason = check_sandbox_bash(f"cd {outside} && touch generated.txt", workspace, [])

    assert reason is not None
    assert "outside the allowed workspace" in reason


def test_sandbox_bash_blocks_git_push_even_with_extra_paths(tmp_path):
    workspace = str(tmp_path / "workspace")
    Path(workspace).mkdir()

    for command in (
        "git push origin main",
        "git -C repo push origin main",
        "GIT_DIR=.git git push origin main",
    ):
        reason = check_sandbox_bash(command, workspace, [str(tmp_path / "cache")])

        assert reason is not None
        assert "git push writes outside" in reason
