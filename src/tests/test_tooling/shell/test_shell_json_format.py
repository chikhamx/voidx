"""Tests that shell tool JSON output is indented for LLM readability."""

from __future__ import annotations

import json

from voidx.tooling.builtin.shell.common import (
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


def test_build_success_result_has_ui_summary():
    result = build_success_result("echo hi", "hello\n", "", 0, "Bash")
    assert result.summary == "ok"




def test_build_failed_result_shortens_stderr_for_ui():
    stderr = (
        "Traceback (most recent call last):\n"
        "  File \"<string>\", line 5, in <module>\n"
        "AttributeError: _convert_responses_chunk_to_generation_chunk. "
        "Did you mean: '_convert_chunk_to_generation_chunk'?\n"
    )
    result = build_success_result("python -c ...", "", stderr, 1, "Bash")

    assert result.summary == "exit 1"
    assert "AttributeError: _convert_responses_chunk_to_generation_chunk." in result.display
    assert "Traceback (most recent call last)" not in result.display
    assert json.loads(result.output)["stderr"] == stderr
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
