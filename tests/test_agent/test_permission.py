"""Tests for permission system — wildcard, evaluate, merge, from_config."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.permission.wildcard import match
from voidx.permission.evaluate import evaluate, from_config, merge
from voidx.permission.schema import Rule, Ruleset


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
