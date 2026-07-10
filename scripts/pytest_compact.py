"""Pytest reporter hooks for concise non-verbose test output."""

from __future__ import annotations

from typing import Any


def pytest_report_teststatus(report: Any, config: Any) -> tuple[str, str, str]:
    """Preserve pytest result categories while suppressing progress characters."""
    when = getattr(report, "when", None)
    was_xfail = getattr(report, "wasxfail", None)

    if was_xfail is not None:
        if report.skipped:
            return "xfailed", "", "XFAIL"
        if report.passed:
            return "xpassed", "", "XPASS"

    if when == "call":
        if report.passed:
            return "passed", "", "PASSED"
        if report.skipped:
            return "skipped", "", "SKIPPED"
        if report.failed:
            return "failed", "", "FAILED"

    if when in {"setup", "teardown"}:
        if report.failed:
            return "error", "", "ERROR"
        if report.skipped:
            return "skipped", "", "SKIPPED"
        return "", "", ""

    if report.failed:
        return "error", "", "ERROR"
    if report.skipped:
        return "skipped", "", "SKIPPED"
    return "", "", ""
