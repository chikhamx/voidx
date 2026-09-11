"""Tests that shell tool text output is formatted cleanly across multiple lines."""

from __future__ import annotations

from voidx.tooling.builtin.shell.common import (
    build_blocked_result,
    build_success_result,
    build_timeout_result,
)


def test_build_success_result_output_is_multiline():
    result = build_success_result("echo hi", "line1\nline2\n", "", 0, "Bash")
    assert result.metadata["ok"] is True
    assert result.metadata["exit_code"] == 0
    # Output should directly contain unescaped multi-line content
    assert "line1\nline2" in result.output
    assert "\\n" not in result.output
    assert "exit code: 0" in result.output


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
    assert "exit code: 1" in result.output
    assert "AttributeError: _convert_responses_chunk_to_generation_chunk." in result.output
    assert "\\n" not in result.output


def test_build_blocked_result_output_is_multiline():
    result = build_blocked_result("rm -rf /", "dangerous")
    assert result.metadata["blocked"] is True
    assert "[blocked]" in result.output
    assert "dangerous" in result.output
    assert "\\n" not in result.output


def test_build_timeout_result_output_is_multiline():
    result = build_timeout_result("sleep 999", 10)
    assert result.metadata["timeout"] is True
    assert "timed out after 10s" in result.output
    assert "\\n" not in result.output


def test_build_blocked_result_includes_static_policy_hint():
    result = build_blocked_result("python x.py", "shell policy deferred: nested interpreter")
    assert result.metadata["blocked"] is True
    assert "rephrasing" in result.output
