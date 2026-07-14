"""Tests for permission system — wildcard, evaluate, merge, from_config."""

import sys
from pathlib import Path


import pytest

from voidx.permission.wildcard import match
from voidx.permission.evaluate import evaluate, from_config, merge
from voidx.permission.schema import Rule, Ruleset
from voidx.permission.rules import BASIC_RULES
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

    def test_basic_rules_explicitly_gate_edit(self):
        result = evaluate("edit", "*", BASIC_RULES)
        assert result.permission == "edit"
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

    assert service.status_label() == "Safe"

    service.allow_silent("bash")
    service.deny_silent("write")

    assert service.status_label() == "Safe"


def test_permission_service_splits_readonly_and_implement_agents():
    service = PermissionService()

    assert service.decide("agent", "voidx") == "allow"
    assert service.decide("agent", "implement") == "ask"


def test_on_intent_is_not_a_runtime_allow_tool(tmp_path):
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

    assert decision.action == "ask"


def test_skill_load_is_allowed_read_tool(tmp_path):
    context = PermissionContext(workspace=str(tmp_path))
    decision = authorize_tool_call(
        {"name": "skill", "args": {"op": "load", "name": "docs"}},
        context,
    )

    assert decision.action == "allow"
    assert classify_tool_call({
        "name": "skill",
        "args": {"op": "load", "name": "docs"},
    }).capability == PermissionCapability.READ_TOOLS


def test_skill_list_is_allowed_read_tool(tmp_path):
    context = PermissionContext(workspace=str(tmp_path))
    decision = authorize_tool_call(
        {"name": "skill", "args": {"op": "list"}},
        context,
    )

    assert decision.action == "allow"
    assert classify_tool_call({
        "name": "skill",
        "args": {"op": "list"},
    }).capability == PermissionCapability.READ_TOOLS


def test_skill_create_is_file_write_and_asks(tmp_path):
    context = PermissionContext(workspace=str(tmp_path))
    decision = authorize_tool_call(
        {"name": "skill", "args": {"op": "create", "name": "docs", "description": "d", "body": "b"}},
        context,
    )

    assert decision.action == "ask"
    assert classify_tool_call({
        "name": "skill",
        "args": {"op": "create", "name": "docs"},
    }).capability == PermissionCapability.FILE_WRITE


@pytest.mark.parametrize("tool_name", ["clarify", "checkpoint", "workflow", "compact"])
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


def test_mcp_tool_execution_requires_permission(tmp_path):
    context = PermissionContext(workspace=str(tmp_path))
    decision = authorize_tool_call(
        {"name": "mcp__demo__send_message_12345678", "args": {"text": "hello"}},
        context,
    )

    assert decision.action == "ask"
    assert decision.capability == PermissionCapability.MCP_TOOLS


def test_permission_service_allows_read_only_lsp_tools_but_asks_for_format():
    service = PermissionService()

    assert service.decide("lsp") == "allow"


def test_permission_service_preset_updates_runtime_decisions():
    service = PermissionService()

    service.set_permission_preset("project_trusted")
    assert service.permission_preset == "project_trusted"
    assert service.status_label() == "Project trusted"
    assert service.decide("manage", "src/app.py") == "allow"
    assert service.decide("write", "src/app.py") == "allow"
    assert service.decide("replace", "src/app.py") == "allow"
    assert service.decide("bash", "pip install requests") == "ask"

    service.set_permission_preset("full_access")
    assert service.permission_preset == "full_access"
    assert service.decide("bash", "python -m pytest") == "ask"
    assert service.status_label() == "Full access"


def test_permission_engine_classifies_basic_capabilities():
    assert classify_tool_call({"name": "read", "args": {"file_path": "x.py"}}).capability == PermissionCapability.READ_TOOLS
    assert classify_tool_call({"name": "manage", "args": {"op": "create", "paths": "x.py"}}).capability == PermissionCapability.FILE_WRITE
    assert classify_tool_call({"name": "write", "args": {"file_path": "x.py"}}).capability == PermissionCapability.FILE_WRITE
    assert classify_tool_call({"name": "replace", "args": {"file_path": "x.py"}}).capability == PermissionCapability.FILE_WRITE
    assert classify_tool_call({"name": "bash", "args": {"command": "ls"}}).capability == PermissionCapability.BASH_READ
    assert classify_tool_call({"name": "bash", "args": {"command": "ls | sort | head"}}).capability == PermissionCapability.BASH_READ
    assert classify_tool_call({"name": "bash", "args": {"command": "pip install requests"}}).capability == PermissionCapability.BASH_WRITE
    assert classify_tool_call({"name": "bash", "args": {"command": "echo hi>out.txt"}}).capability == PermissionCapability.BASH_WRITE
    assert classify_tool_call({"name": "bash", "args": {"command": "grep foo a.txt | xargs rm"}}).capability == PermissionCapability.BASH_WRITE
    assert classify_tool_call({"name": "bash", "args": {"command": "find . -delete"}}).capability == PermissionCapability.BASH_WRITE
    assert classify_tool_call({"name": "bash", "args": {"command": "git branch new-feature"}}).capability == PermissionCapability.BASH_WRITE
    assert classify_tool_call({"name": "git", "args": {"args": "status"}}).capability == PermissionCapability.GIT_READ
    assert classify_tool_call({"name": "git", "args": {"args": "commit"}}).capability == PermissionCapability.GIT_WRITE
    readonly_agent = classify_tool_call({"name": "agent", "args": {"agent": "explore"}})
    assert readonly_agent.capability == PermissionCapability.AGENT_READONLY
    assert readonly_agent.pattern == "voidx"
    implement_agent = classify_tool_call({"name": "agent", "args": {"agent": "implement"}})
    assert implement_agent.capability == PermissionCapability.AGENT_IMPLEMENT
    assert implement_agent.pattern == "implement"
    mode_implement_agent = classify_tool_call({"name": "agent", "args": {"agent": "voidx", "mode": "implement"}})
    assert mode_implement_agent.capability == PermissionCapability.AGENT_IMPLEMENT
    assert mode_implement_agent.pattern == "implement"
    assert classify_tool_call({"name": "workflow", "args": {}}).capability == PermissionCapability.READ_TOOLS
    assert classify_tool_call({"name": "advance_workflow", "args": {}}).name == "advance_workflow"
    assert classify_tool_call({"name": "compact", "args": {}}).capability == PermissionCapability.READ_TOOLS


def test_permission_engine_default_strategy_and_plan_overlay(tmp_path):
    context = PermissionContext(workspace=str(tmp_path))

    assert authorize_tool_call({"name": "read", "args": {"file_path": "x.py"}}, context).action == "allow"
    assert authorize_tool_call({"name": "bash", "args": {"command": "ls"}}, context).action == "allow"
    script_decision = authorize_tool_call({"name": "bash", "args": {"command": "./test.py"}}, context)
    assert script_decision.action == "ask"
    assert script_decision.source == "sandbox"
    assert authorize_tool_call({"name": "git", "args": {"args": "status"}}, context).action == "allow"
    assert authorize_tool_call({"name": "git", "args": {"args": "commit"}}, context).action == "ask"
    assert authorize_tool_call({"name": "manage", "args": {"op": "create", "paths": "x.py"}}, context).action == "ask"
    assert authorize_tool_call({"name": "agent", "args": {"agent": "implement"}}, context).action == "ask"
    assert authorize_tool_call({"name": "agent", "args": {"agent": "voidx", "mode": "implement"}}, context).action == "ask"

    plan = PermissionContext(workspace=str(tmp_path), interaction_mode="plan")
    safe_bash = authorize_tool_call({"name": "bash", "args": {"command": "ls"}}, plan)
    unsafe_bash = authorize_tool_call({"name": "bash", "args": {"command": "pip install requests"}}, plan)
    git_read = authorize_tool_call({"name": "git", "args": {"args": "diff"}}, plan)
    git_write = authorize_tool_call({"name": "git", "args": {"args": "restore"}}, plan)
    edit = authorize_tool_call({"name": "write", "args": {"file_path": "x.py"}}, plan)
    replace = authorize_tool_call({"name": "replace", "args": {"file_path": "x.py"}}, plan)
    implement = authorize_tool_call({"name": "agent", "args": {"agent": "implement"}}, plan)
    mode_implement = authorize_tool_call({"name": "agent", "args": {"agent": "voidx", "mode": "implement"}}, plan)

    assert safe_bash.action == "allow"
    assert unsafe_bash.action == "deny"
    assert git_read.action == "allow"
    assert git_write.action == "deny"
    assert edit.action == "deny"
    assert replace.action == "deny"
    assert implement.action == "deny"
    assert mode_implement.action == "deny"


def test_permission_engine_plan_mode_uses_sandbox_source(tmp_path):
    """plan 模式复用 read-only 沙箱逻辑，deny 的 source 应为 'sandbox' 而非 'mode'。"""
    plan = PermissionContext(workspace=str(tmp_path), interaction_mode="plan")
    unsafe_bash = authorize_tool_call({"name": "bash", "args": {"command": "pip install requests"}}, plan)
    git_write = authorize_tool_call({"name": "git", "args": {"args": "restore"}}, plan)
    edit = authorize_tool_call({"name": "write", "args": {"file_path": "x.py"}}, plan)
    implement = authorize_tool_call({"name": "agent", "args": {"agent": "implement"}}, plan)

    assert unsafe_bash.action == "deny"
    assert unsafe_bash.source == "sandbox"
    assert git_write.action == "deny"
    assert git_write.source == "sandbox"
    assert edit.action == "deny"
    assert edit.source == "sandbox"
    assert implement.action == "deny"
    assert implement.source == "sandbox"


def test_permission_engine_policy_presets(tmp_path):
    project_trusted = PermissionContext(
        workspace=str(tmp_path),
        permission_preset="project_trusted",
    )
    full_access = PermissionContext(
        workspace=str(tmp_path),
        sandbox_mode="danger-full-access",
        approval_policy="never",
    )

    safe_edit = authorize_tool_call({"name": "manage", "args": {"op": "create", "paths": "x.py"}}, PermissionContext(workspace=str(tmp_path)))
    full_access_edit = authorize_tool_call(
        {"name": "manage", "args": {"op": "create", "paths": "x.py"}},
        PermissionContext(workspace=str(tmp_path), permission_preset="full_access", sandbox_mode="danger-full-access"),
    )

    assert safe_edit.action == "ask"
    assert full_access_edit.action == "allow"


def test_permission_engine_read_only_sandbox_allows_read_bash_but_asks_for_writes(tmp_path):
    context = PermissionContext(workspace=str(tmp_path), sandbox_mode="read-only")

    assert authorize_tool_call({"name": "bash", "args": {"command": "ls"}}, context).action == "allow"
    assert authorize_tool_call({"name": "git", "args": {"args": "status"}}, context).action == "allow"
    assert authorize_tool_call({"name": "git", "args": {"args": "commit"}}, context).action == "ask"
    bash = authorize_tool_call({"name": "bash", "args": {"command": "pip install requests"}}, context)
    assert bash.action == "ask"
    assert bash.allowed_scopes == ("once",)
    assert authorize_tool_call({"name": "manage", "args": {"op": "create", "paths": "x.py"}}, context).action == "ask"
    assert authorize_tool_call({"name": "write", "args": {"file_path": "x.py"}}, context).action == "ask"
    assert authorize_tool_call({"name": "replace", "args": {"file_path": "x.py"}}, context).action == "ask"


def test_permission_engine_asks_for_manage_in_read_only_sandbox(tmp_path):
    context = PermissionContext(workspace=str(tmp_path), sandbox_mode="read-only")

    assert authorize_tool_call({"name": "manage", "args": {"op": "create", "paths": "x.py"}}, context).action == "ask"
    assert authorize_tool_call({"name": "manage", "args": {"op": "delete", "paths": ["x.py"]}}, context).action == "ask"
    assert authorize_tool_call(
        {"name": "manage", "args": {"op": "move", "moves": [{"src": "x.py", "dest": "y.py"}]}},
        context,
    ).action == "ask"


def test_permission_engine_workspace_write_checks_manage_paths(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    context = PermissionContext(workspace=str(workspace), sandbox_mode="workspace-write")

    inside = authorize_tool_call({"name": "manage", "args": {"op": "create", "paths": "safe.py"}}, context)
    create_outside = authorize_tool_call({"name": "manage", "args": {"op": "create", "paths": str(outside / "x.py")}}, context)
    create_batch_outside = authorize_tool_call(
        {"name": "manage", "args": {"op": "create", "paths": ["safe.py", str(outside / "batch.py")]}},
        context,
    )
    move_src_outside = authorize_tool_call(
        {"name": "manage", "args": {"op": "move", "moves": [{"src": str(outside / "x.py"), "dest": "safe.py"}]}},
        context,
    )
    move_dest_outside = authorize_tool_call(
        {"name": "manage", "args": {"op": "move", "moves": [{"src": "safe.py", "dest": str(outside / "x.py")}]}},
        context,
    )

    assert inside.action != "deny"
    assert create_outside.action == "ask"
    assert "outside the allowed workspace" in create_outside.reason
    assert create_batch_outside.action == "ask"
    assert "outside the allowed workspace" in create_batch_outside.reason
    assert move_src_outside.action == "ask"
    assert "outside the allowed workspace" in move_src_outside.reason
    assert move_dest_outside.action == "ask"
    assert "outside the allowed workspace" in move_dest_outside.reason


def test_sandbox_bash_tracks_cd_before_relative_write(tmp_path):
    workspace = str(tmp_path / "workspace")
    outside = tmp_path / "outside"
    Path(workspace).mkdir()
    outside.mkdir()

    reason = check_sandbox_bash(f"cd {outside} && touch generated.txt", workspace, [])

    assert reason is not None
    assert "outside the allowed workspace" in reason


def test_sandbox_bash_tracks_cd_with_quoted_path_before_relative_write(tmp_path):
    workspace = str(tmp_path / "workspace")
    outside = tmp_path / "outside"
    Path(workspace).mkdir()
    outside.mkdir()

    reason = check_sandbox_bash(f'cd "{outside}" && touch generated.txt', workspace, [])

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


def test_is_safe_bash_preserves_windows_backslash_path():
    """is_safe_bash must not eat backslash paths on Windows.

    shlex posix=True treats backslash as escape, turning C:\\Users\\foo
    into C:Usersfoo. Must use posix=False to match sandbox.py behavior.
    """
    from voidx.permission.rules import is_safe_bash, _shell_words

    # A read-only command with a Windows path should be safe
    assert is_safe_bash("cat C:\\Users\\foo\\app.py") is True

    # The path must be preserved in the parsed words
    words = _shell_words("cat C:\\Users\\foo\\app.py")
    assert words is not None
    assert "C:\\Users\\foo\\app.py" in words


class TestDataDirInjection:
    """DATA_DIR should be auto-injected into runtime writable grants."""

    def test_build_permission_service_includes_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        from voidx.config import Config, ModelConfig
        from voidx.agent.graph.wiring import build_permission_service
        from voidx.memory.store import DATA_DIR

        config = Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        )
        service = build_permission_service(config, notifier=lambda _msg: None)
        expected = str(DATA_DIR.resolve())
        assert expected in service.sandbox_writable_dirs

    def test_build_permission_service_preserves_user_extra_paths(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        from voidx.config import Config, ModelConfig
        from voidx.agent.graph.wiring import build_permission_service

        user_extra = str(tmp_path / "custom")
        config = Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
            sandbox_writable_dirs=[user_extra],
        )
        service = build_permission_service(config, notifier=lambda _msg: None)
        assert user_extra in service.sandbox_writable_dirs

    def test_execution_policy_from_config_includes_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        from voidx.config import Config, ModelConfig
        from voidx.agent.runtime_context import ExecutionPolicy
        from voidx.memory.store import DATA_DIR

        config = Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        )
        policy = ExecutionPolicy.from_config(config)
        expected = str(DATA_DIR.resolve())
        assert expected in policy.extra_write_paths


def test_engine_defers_approvable_read(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "file.txt"
    target.write_text("hello\n", encoding="utf-8")
    context = PermissionContext(workspace=str(workspace), sandbox_mode="workspace-write")

    decision = authorize_tool_call({"name": "read", "args": {"file_path": str(target)}}, context)

    assert decision.action == "ask"
    assert decision.source == "sandbox"


def test_engine_denies_non_approvable_tool(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    context = PermissionContext(workspace=str(workspace), sandbox_mode="workspace-write")

    decision = authorize_tool_call(
        {"name": "manage", "args": {"op": "create", "paths": str(outside / "x.txt")}},
        context,
    )

    assert decision.action == "ask"
    assert "outside the allowed workspace" in decision.reason
