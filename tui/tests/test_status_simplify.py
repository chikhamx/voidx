"""Tests for simplified status bar field formatting."""

from tui_helpers import *  # noqa: F403

from types import SimpleNamespace

from rich.console import Console

from voidx.llm.usage import UsageStats
from voidx.ui.commands import COMMANDS
from voidx.ui.output.types import UiStatus
from voidx_cli import PureTui


def _make_status(tmp_path, **overrides):
    defaults = dict(
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        workspace=str(tmp_path),
        reasoning_effort="xhigh",
        sandbox_label=lambda: "w-write",
        approval_label=lambda: "on-fail",
        approval_reviewer_label=lambda: "user",
        interaction_mode=lambda: "auto",
        debug=lambda: False,
        goal_label=lambda: "",
        active_workflows=lambda: [],
        usage_stats=None,
        context_limit=200000,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _status_plain(tui, width=100):
    text = tui._status_summary_text(width)
    return text.plain


# ── model segment: no provider prefix ──────────────────────────────

def test_model_segment_excludes_provider(tmp_path):
    tui = PureTui(_make_status(tmp_path), COMMANDS)
    plain = _status_plain(tui)
    assert "anthropic/" not in plain
    assert "claude-sonnet-4-20250514" in plain
    assert "xhigh" in plain


# ── policy segment: no permission_label ────────────────────────────

def test_policy_segment_excludes_permission_label(tmp_path):
    status = _make_status(tmp_path)
    tui = PureTui(status, COMMANDS)
    plain = _status_plain(tui)
    assert "default" not in plain.lower()
    assert "w-write" in plain
    assert "on-fail" in plain


def test_policy_segment_includes_reviewer_when_not_user(tmp_path):
    status = _make_status(
        tmp_path,
        approval_reviewer_label=lambda: "reviewer",
    )
    tui = PureTui(status, COMMANDS)
    plain = _status_plain(tui)
    assert "reviewer" in plain


# ── state segment: no plan_mode, auto hidden ───────────────────────

def test_state_segment_hidden_when_auto(tmp_path):
    tui = PureTui(_make_status(tmp_path, interaction_mode=lambda: "auto"), COMMANDS)
    plain = _status_plain(tui)
    # auto should not appear as a state segment
    assert "| auto" not in plain


def test_state_segment_shows_plan_without_duplication(tmp_path):
    tui = PureTui(_make_status(tmp_path, interaction_mode=lambda: "plan"), COMMANDS)
    plain = _status_plain(tui)
    assert "plan" in plain
    # Should not duplicate: "plan plan" is a bug
    assert "plan plan" not in plain


def test_state_segment_shows_debug(tmp_path):
    tui = PureTui(_make_status(tmp_path, debug=lambda: True), COMMANDS)
    plain = _status_plain(tui)
    assert "debug" in plain


# ── usage segment: compact format, no total ────────────────────────

def test_usage_segment_compact_format(tmp_path):
    stats = UsageStats()
    stats.context_limit = 200_000
    stats.total_input_tokens = 10_000
    stats.total_output_tokens = 1_000
    stats.begin_turn()
    stats.update_context(10_000)
    stats.total_input_tokens = 126_100
    stats.total_output_tokens = 1_043
    status = _make_status(tmp_path, usage_stats=stats)
    tui = PureTui(status, COMMANDS)
    plain = _status_plain(tui)
    assert "10k/200k" in plain
    # No verbose labels
    assert "ctx " not in plain
    assert "cache " not in plain
    assert "total " not in plain


# ── goal_type / goal_awaiting: dead fields removed ─────────────────

def test_status_works_without_goal_type_field(tmp_path):
    """UiStatus should no longer have goal_type or goal_awaiting_approval fields."""
    status = UiStatus(
        model="m",
        provider="p",
        workspace=str(tmp_path),
        session_title="t",
        context_limit=200000,
        debug=lambda: False,
        plan_mode=lambda: False,
    )
    assert not hasattr(status, "goal_type")
    assert not hasattr(status, "goal_awaiting_approval")
    assert not hasattr(status, "permission_label")


# ── variant priority: workflow before usage at narrow widths ───────

def test_workflow_shown_at_narrow_width_with_long_model(tmp_path):
    """At 80 cols with a long model name, workflow should still appear."""
    status = _make_status(
        tmp_path,
        active_workflows=lambda: ["verify"],
        goal_label=lambda: "fix bug",
    )
    stats = UsageStats()
    stats.context_limit = 200_000
    stats.total_input_tokens = 10_000
    stats.total_output_tokens = 1_000
    stats.begin_turn()
    stats.update_context(10_000)
    stats.total_input_tokens = 126_100
    stats.total_output_tokens = 1_043
    status.usage_stats = stats
    tui = PureTui(status, COMMANDS)
    plain = _status_plain(tui, width=80)
    assert "verify" in plain
