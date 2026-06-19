from tui_helpers import *  # noqa: F403

import asyncio
import os
import shutil
import sys
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.console import Console

from voidx.llm.usage import UsageStats
from voidx.ui.commands import COMMANDS
from voidx.ui.output.dock import dock
from voidx.ui.tui import PureTui, _rendered_row_count
from voidx.ui.tui.state import InputState, RenderState

def test_pure_tui_groups_runtime_state(tmp_path):
    tui = _tui(tmp_path)

    assert isinstance(tui._input_state, InputState)
    assert isinstance(tui._render_state, RenderState)

    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    assert tui._input_state.lines == ["hello"]
    assert tui._input_state.cursor_col == 5
    assert tui._input_lines == ["hello"]


def test_choice_render_handles_unselected_items_and_details(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [("y", "y", ""), ("n", "n", "")]
    tui._choice_prompt = "Allow [tool]?"
    tui._choice_selected = 0
    tui._choice_details = [{"name": "write", "pattern": "src/[file].py"}]

    # Previously unselected items generated invalid Rich markup: []No[/].
    renderable = tui._render_impl()

    assert renderable is not None


def test_choice_move_marks_selection_only_render(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [("y", "y", ""), ("n", "n", "")]
    tui._choice_selected = 0

    tui._move_choice(1)

    assert tui._choice_selected == 1
    assert tui._choice_selection_render_pending is True


def test_choice_move_single_option_does_not_request_render(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [("y", "y", "")]
    tui._choice_selected = 0

    tui._move_choice(1)

    assert tui._choice_selected == 0
    assert tui._choice_selection_render_pending is False


def test_choice_selection_only_render_does_not_clear_to_screen_end(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    tui = _tui(tmp_path)
    tui._tty = True
    tui._has_rendered_frame = True
    tui._last_bottom_start_row = 7
    tui._last_frame_rows = 14
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._active_choice = [("review", "review", ""), ("implement", "implement", "")]
    tui._choice_prompt = "Intent?"
    tui._choice_selected = 0
    ansi = tui._capture_renderable(tui._render_bottom_impl(), tui._frame_width())
    tui._last_bottom_rows = _rendered_row_count(ansi)

    tui._choice_selected = 1

    assert tui._render_choice_selection_region() is True
    assert "\x1b[J" not in fake_stdout.text
    assert "\x1b[K" in fake_stdout.text
    assert "\x1b[7;1H" in fake_stdout.text


def test_choice_selection_only_render_falls_back_when_row_count_changes(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._has_rendered_frame = True
    tui._last_bottom_start_row = 7
    tui._last_frame_rows = 14
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._active_choice = [("review", "review", ""), ("implement", "implement", "")]
    tui._choice_prompt = "Intent?"
    tui._choice_selected = 0
    ansi = tui._capture_renderable(tui._render_bottom_impl(), tui._frame_width())
    tui._last_bottom_rows = _rendered_row_count(ansi) + 1
    tui._choice_selection_render_pending = True
    tui._input_region_render_pending = True
    calls: list[str] = []
    monkeypatch.setattr(tui, "_render_input_region", lambda: calls.append("input"))

    tui._render_after_input()

    assert calls == ["input"]
    assert tui._choice_selection_render_pending is False
    assert tui._input_region_render_pending is False


def test_status_summary_renders_model_policy_usage_and_goal(tmp_path):
    stats = UsageStats()
    stats.update_context(12_345, limit=128_000)
    stats.last_input_tokens = 12_345
    stats.last_output_tokens = 678
    stats.total_input_tokens = 12_345
    stats.total_output_tokens = 678
    stats.total_calls = 1
    status = SimpleNamespace(
        provider="mimo",
        model="mimo-v2.5",
        workspace=str(tmp_path),
        context_limit=128_000,
        debug=lambda: True,
        plan_mode=lambda: False,
        interaction_mode=lambda: "goal",
        goal_label=lambda: "ship pure tui",
        goal_type=lambda: "feature",
        goal_awaiting_approval=lambda: False,
        reasoning_effort="xhigh",
        permission_label=lambda: "default",
        sandbox_label=lambda: "w-write",
        approval_label=lambda: "on-fail",
        approval_reviewer_label=lambda: "auto",
        usage_stats=stats,
    )
    tui = PureTui(status, COMMANDS)

    summary = tui._status_summary(200)

    assert "mimo/mimo-v2.5 xhigh" in summary
    assert "default w-write on-fail auto" in summary
    assert "goal" in summary
    assert "ctx 12.3k/128k" in summary
    assert "cache -- total 13.0k" in summary
    assert "↑" not in summary
    assert "↓" not in summary
    assert " in " not in summary
    assert " out " not in summary
    assert "goal feature ship pure tui" in summary


def test_status_summary_text_applies_semantic_styles(tmp_path):
    stats = UsageStats()
    stats.update_context(12_345, limit=128_000)
    stats.last_input_tokens = 12_345
    stats.last_output_tokens = 678
    stats.total_input_tokens = 12_345
    stats.total_output_tokens = 678
    status = SimpleNamespace(
        provider="mimo",
        model="mimo-v2.5",
        workspace=str(tmp_path),
        interaction_mode=lambda: "auto",
        goal_label=lambda: "ship",
        goal_type=lambda: "feature",
        goal_awaiting_approval=lambda: False,
        reasoning_effort="xhigh",
        permission_label=lambda: "default",
        sandbox_label=lambda: "w-write",
        approval_label=lambda: "on-fail",
        usage_stats=stats,
    )
    tui = PureTui(status, COMMANDS)

    text = tui._status_summary_text(200)

    assert text.plain.startswith("  mimo/mimo-v2.5 xhigh")
    assert "#6CB6FF" in _styles_covering(text, "mimo/mimo-v2.5 xhigh")
    assert "#57AB5A" in _styles_covering(text, "default w-write on-fail")
    assert "#D77757" in _styles_covering(text, "auto")
    assert "#56D4DD" in _styles_covering(text, "ctx 12.3k/128k")
    assert "#C698F0" in _styles_covering(text, "goal feature ship")
    assert "#4B5563" in _styles_covering(text, "|")


def test_status_summary_renders_active_workflow_name(tmp_path):
    status = SimpleNamespace(
        provider="mimo",
        model="mimo-v2.5",
        workspace=str(tmp_path),
        interaction_mode=lambda: "auto",
        active_workflows=lambda: ["tdd"],
    )
    tui = PureTui(status, COMMANDS)

    summary = tui._status_summary(120)

    assert "tdd" in summary
    assert "wf tdd" not in summary


def test_status_summary_prefers_active_workflow_over_usage_when_narrow(tmp_path):
    stats = UsageStats(
        context_tokens=12_300,
        context_limit=128_000,
        total_input_tokens=30_000,
        total_output_tokens=15_600,
    )
    status = SimpleNamespace(
        provider="openai",
        model="gpt-5-codex",
        workspace=str(tmp_path),
        interaction_mode=lambda: "auto",
        active_workflows=lambda: ["debug"],
        permission_label=lambda: "default",
        sandbox_label=lambda: "w-write",
        approval_label=lambda: "on-fail",
        approval_reviewer_label=lambda: "user",
        usage_stats=stats,
        reasoning_effort="xhigh",
    )
    tui = PureTui(status, COMMANDS)

    summary = tui._status_summary(100)

    assert "debug" in summary
    assert "wf debug" not in summary
    assert "ctx 12.3k/128k" not in summary


def test_status_summary_text_renders_workflow_name_as_rainbow(tmp_path):
    status = SimpleNamespace(
        provider="mimo",
        model="mimo-v2.5",
        workspace=str(tmp_path),
        interaction_mode=lambda: "auto",
        active_workflows=lambda: ["review"],
    )
    tui = PureTui(status, COMMANDS)

    text = tui._status_summary_text(120)
    assert "review" in text.plain
    assert "wf review" not in text.plain
    start = text.plain.index("review")
    styles = {
        str(span.style)
        for index in range(start, start + len("review"))
        for span in text.spans
        if span.start <= index < span.end
    }

    assert len(styles) >= 4
    assert "workflow review" not in text.plain


def test_active_workflow_names_extracts_active_runs():
    from voidx.agent.graph.workflow_utils import active_workflow_names as _active_workflow_names
    from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus

    state = SimpleNamespace(
        workflow_runs={
            "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
            "verify": WorkflowRunState(name="verify", status=WorkflowRunStatus.PENDING),
        }
    )

    assert _active_workflow_names(state) == ["tdd"]


def test_status_summary_text_fallback_uses_dim_style(tmp_path):
    status = SimpleNamespace(
        provider="anthropic",
        model="claude-sonnet-4",
        workspace=str(tmp_path),
        reasoning_effort="xhigh",
        permission_label=lambda: "accept-edits",
    )
    tui = PureTui(status, COMMANDS)

    text = tui._status_summary_text(18)

    assert cell_len(text.plain) <= 18
    assert str(text.style) == "#8F9BA8"
    assert text.spans == []


def test_status_summary_omits_agent_step_from_dock(tmp_path):
    tui = _tui(tmp_path)
    dock.record_status("agent:-1:progress", "Agent step 1/50", stage="agent step")

    summary = tui._status_summary(80)

    assert "step 1/50" not in summary
    assert "Agent step" not in summary


def test_status_summary_degrades_to_fit_width(tmp_path):
    status = SimpleNamespace(
        provider="anthropic",
        model="claude-sonnet-4",
        workspace=str(tmp_path),
        reasoning_effort="xhigh",
        permission_label=lambda: "accept-edits",
    )
    tui = PureTui(status, COMMANDS)

    summary = tui._status_summary(18)

    assert len(summary) <= 18
    assert summary.startswith("  anthropic")


def test_status_summary_degrades_by_display_width_for_cjk(tmp_path):
    status = SimpleNamespace(
        provider="模型",
        model="超宽模型",
        workspace=str(tmp_path),
        reasoning_effort="推理",
        permission_label=lambda: "接受编辑",
    )
    tui = PureTui(status, COMMANDS)

    summary = tui._status_summary(10)

    assert cell_len(summary) <= 10


def test_status_summary_is_empty_without_model_status(tmp_path):
    tui = _tui(tmp_path)

    assert tui._status_summary(80) == ""
    assert tui._render_hint_lines() == []


def test_status_summary_reuses_cache_until_marked_dirty(tmp_path):
    calls = {"permission": 0}

    def permission_label() -> str:
        calls["permission"] += 1
        return f"perm-{calls['permission']}"

    status = SimpleNamespace(
        provider="mimo",
        model="mimo-v2.5",
        workspace=str(tmp_path),
        reasoning_effort="xhigh",
        permission_label=permission_label,
    )
    tui = PureTui(status, COMMANDS)

    first = tui._status_summary(120)
    second = tui._status_summary(120)

    assert first == second
    assert calls["permission"] == 1

    tui._mark_status_summary_dirty()
    third = tui._status_summary(120)

    assert "perm-2" in third
    assert calls["permission"] == 2


def test_busy_activity_line_renders_below_temporary_agent_not_status(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: 103.8)
    status = SimpleNamespace(
        provider="mimo",
        model="mimo-v2.5",
        workspace=str(tmp_path),
        interaction_mode=lambda: "auto",
    )
    tui = PureTui(status, COMMANDS)
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Cogitating"
    tui._input_lines = ["hello"]
    tui._cursor_col = len("hello")
    dock.begin_capture()
    dock.ensure_agent()
    dock.start_tool(
        "Searching",
        "",
        tool_name="mcp__tavily__tavily_search",
        raw_args={"query": "recent AI major events news 2025 2026"},
    )
    dock.set_todo_state(
        "0/1 done · 1 active · 0 pending",
        [{"content": "active task", "status": "in_progress"}],
    )

    lines = _render_lines(tui, width=80)
    tool_index = next(i for i, line in enumerate(lines) if "Search(" in line)
    todo_index = next(i for i, line in enumerate(lines) if "Todo: 0/1 done" in line)
    busy_index = next(i for i, line in enumerate(lines) if "Cogitating" in line)
    input_index = next(i for i, line in enumerate(lines) if line.strip() == "❯ hello")
    status_index = next(i for i, line in enumerate(lines) if "mimo/mimo-v2.5" in line)
    status = tui._status_summary_text(120)
    rendered = "\n".join(_rich_plain(line) for line in lines)

    assert tool_index < todo_index < busy_index < input_index < status_index
    assert "Cogitating (3s)" in rendered
    assert rendered.count("Cogitating") == 1
    assert "voidx" not in rendered
    assert "Cogitating" not in status.plain
    assert "busy" not in status.plain
    assert "auto" in status.plain


