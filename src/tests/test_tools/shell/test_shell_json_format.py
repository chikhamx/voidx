"""Tests that shell tool JSON output is indented for LLM readability."""

from __future__ import annotations

import json

from voidx.tools.shell.common import (
    build_blocked_result,
    build_success_result,
    build_timeout_result,
)


def test_build_success_result_json_is_indented():
    result = build_success_result("echo hi", "hello\n", "", 0, "Bash")
    parsed = json.loads(result.output)
    assert parsed["ok"] is True
    assert parsed["stdout"] == "hello\n"
    assert "\n  " in result.output, "JSON output should be indented with newlines"


def test_build_blocked_result_json_is_indented():
    result = build_blocked_result("rm -rf /", "dangerous")
    parsed = json.loads(result.output)
    assert parsed["blocked"] is True
    assert "\n  " in result.output, "JSON output should be indented with newlines"


def test_build_timeout_result_json_is_indented():
    result = build_timeout_result("sleep 999", 10)
    parsed = json.loads(result.output)
    assert parsed["timeout"] is True
    assert "\n  " in result.output, "JSON output should be indented with newlines"


def test_build_blocked_result_includes_static_policy_hint():
    result = build_blocked_result("python x.py", "shell policy deferred: nested interpreter")
    parsed = json.loads(result.output)
    assert parsed["blocked"] is True
    assert "rephrasing" in parsed["stderr"]
