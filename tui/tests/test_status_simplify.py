"""Tests for simplified status bar field formatting."""

from tui_helpers import *  # noqa: F403

from types import SimpleNamespace

from rich.cells import cell_len
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
        sandbox_label=lambda: "safe",
        approval_label=lambda: "",
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


def _usage_stats() -> UsageStats:
    stats = UsageStats()
    stats.context_limit = 200_000
    stats.total_input_tokens = 10_000
    stats.total_output_tokens = 1_000
    stats.begin_turn()
    stats.update_context(46_300)
    stats.total_input_tokens = 1_899_000
    stats.total_output_tokens = 1_000
    stats.total_cache_read_tokens = 399_000
    stats.total_cache_metric_calls = 1
    return stats


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
    assert "safe" in plain




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


# ── usage segment: compact format with total ───────────────────────

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
    assert plain.endswith("10k/200k -- 127.1k")
    # No verbose labels
    assert "ctx " not in plain
    assert "cache " not in plain


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


def test_usage_is_right_aligned_and_preserved_with_long_goal(tmp_path):
    status = _make_status(
        tmp_path,
        active_workflows=lambda: ["重命名"],
        goal_label=lambda: "将某个文件/模块重命名并移动到 file 目录下，需确认具体对象并同步更新 import 路径",
        usage_stats=_usage_stats(),
    )
    tui = PureTui(status, COMMANDS)
    width = 120

    plain = _status_plain(tui, width=width)

    assert plain.endswith("46.3k/200k 21% 1.9m")
    assert "\n" not in plain
    assert cell_len(plain) == width
    assert "重命名" in plain
    assert "…" in plain
    assert "import 路径" not in plain


def test_usage_survives_when_left_and_middle_are_too_wide(tmp_path):
    status = _make_status(
        tmp_path,
        model="astron-code-latest",
        reasoning_effort="xhigh",
        active_workflows=lambda: ["workflow-with-a-very-long-name"],
        goal_label=lambda: "x" * 200,
        usage_stats=_usage_stats(),
    )
    tui = PureTui(status, COMMANDS)
    width = 72

    plain = _status_plain(tui, width=width)

    assert plain.endswith("46.3k/200k 21% 1.9m")
    assert "\n" not in plain
    assert cell_len(plain) == width


def test_pinned_usage_status_keeps_segment_styles(tmp_path):
    status = _make_status(
        tmp_path,
        active_workflows=lambda: ["重命名"],
        goal_label=lambda: "将某个文件/模块重命名并移动到 file 目录下，需确认具体对象并同步更新 import 路径",
        usage_stats=_usage_stats(),
    )
    tui = PureTui(status, COMMANDS)

    text = tui._status_summary_text(120)

    assert "#56D4DD" in _styles_covering(text, "46.3k/200k 21% 1.9m")
    assert "#C698F0" in _styles_covering(text, "…")
