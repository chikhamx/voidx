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
    tui._active_choice = [
        ("Yes [once]", "y", "Allow [only] once"),
        ("No", "n", "Deny"),
    ]
    tui._choice_prompt = "Allow [tool]?"
    tui._choice_selected = 0
    tui._choice_details = [{"name": "write", "pattern": "src/[file].py"}]

    # Previously unselected items generated invalid Rich markup: []No[/].
    renderable = tui._render_impl()

    assert renderable is not None


def test_choice_move_marks_selection_only_render(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [
        ("Yes", "y", "Allow"),
        ("No", "n", "Deny"),
    ]
    tui._choice_selected = 0

    tui._move_choice(1)

    assert tui._choice_selected == 1
    assert tui._choice_selection_render_pending is True


def test_choice_move_single_option_does_not_request_render(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [("Yes", "y", "Allow")]
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
    tui._active_choice = [
        ("Review", "review", "Inspect the design"),
        ("Implement", "implement", "Apply the change"),
    ]
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
    tui._active_choice = [
        ("Review", "review", "Inspect the design"),
        ("Implement", "implement", "Apply the change"),
    ]
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


def test_todo_busy_and_choice_panel_render_once_in_full_frame(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Working"
    tui._input_lines = ["review change"]
    tui._cursor_col = len("review change")
    tui._active_choice = [
        ("Review", "review", "Inspect the change"),
        ("Implement", "implement", "Apply the change"),
    ]
    tui._choice_prompt = "Intent?"
    tui._choice_selected = 0
    dock.begin_capture()
    dock.ensure_agent()
    dock.set_todo_state(
        "0/2 done · 1 active · 1 pending",
        [
            {"content": "inspect behavior", "status": "in_progress"},
            {"content": "write regression", "status": "pending"},
        ],
    )

    rendered = "\n".join(_rich_plain(line) for line in _render_lines(tui, width=80))

    assert rendered.count("Todo:") == 1
    assert rendered.count("Working") == 1
    assert rendered.count("Intent?") == 1
    assert rendered.index("Todo:") < rendered.index("Working") < rendered.index("Intent?")


def test_choice_selection_only_render_skips_todo_and_busy_lines(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: 105.0)
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    tui = _tui(tmp_path)
    tui._tty = True
    tui._has_rendered_frame = True
    tui._last_bottom_start_row = 7
    tui._last_frame_rows = 16
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Working"
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._active_choice = [
        ("Review", "review", "Inspect the change"),
        ("Implement", "implement", "Apply the change"),
    ]
    tui._choice_prompt = "Intent?"
    tui._choice_selected = 0
    dock.set_todo_state(
        "0/1 done · 1 active · 0 pending",
        [{"content": "active task", "status": "in_progress"}],
    )
    ansi = tui._capture_renderable(tui._render_bottom_impl(), tui._frame_width())
    tui._last_bottom_rows = _rendered_row_count(ansi)

    tui._choice_selected = 1

    assert tui._render_choice_selection_region() is True
    output = _rich_plain(fake_stdout.text)
    assert "Intent?" in output
    assert "Todo:" not in output
    assert "Working" not in output


def test_todo_busy_and_text_prompt_render_once_in_full_frame(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Working"
    tui._active_text_prompt = "Name?"
    tui._input_lines = ["default"]
    tui._cursor_col = len("default")
    dock.begin_capture()
    dock.ensure_agent()
    dock.set_todo_state(
        "0/1 done · 1 active · 0 pending",
        [{"content": "active task", "status": "in_progress"}],
    )

    rendered = "\n".join(_rich_plain(line) for line in _render_lines(tui, width=80))

    assert rendered.count("Todo:") == 1
    assert rendered.count("Working") == 1
    assert rendered.count("Name?") == 1
    assert rendered.index("Todo:") < rendered.index("Working") < rendered.index("Name?")


def test_agent_placeholder_keeps_stream_reusable(tmp_path):
    dock.begin_capture()
    dock.ensure_agent()

    dock.set_stream("final answer")

    rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(80))

    assert "final answer" in rendered
    assert "● voidx" not in rendered
    assert "Cogitating" not in rendered


def test_agent_placeholder_replaces_legacy_working_header(tmp_path):
    dock.begin_capture()
    agent = dock.ensure_agent()
    agent.header = "[#EBCB8B]●[/#EBCB8B] Working [dim](12s)[/dim]"

    dock.set_stream("final answer")

    rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(80))

    assert "final answer" in rendered
    assert "Working" not in rendered


def test_busy_activity_tick_noops_without_rendered_frame(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: 70.0)
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 0.0

    status = tui._status_summary_text(20)

    assert tui._render_busy_activity_tick() is False
    assert "Cogitating" not in status.plain


def test_busy_activity_label_rotates_centered_glyphs(tmp_path, monkeypatch):
    now = {"value": 100.0}
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: now["value"])
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Pondering"

    assert tui._busy_activity_label().startswith("◐ Pondering (0s)")
    tui._busy_activity_tick = 1
    now["value"] = 101.0
    assert tui._busy_activity_label().startswith("◓ Pondering (1s)")
    tui._busy_activity_tick = 3
    now["value"] = 103.0
    assert tui._busy_activity_label().startswith("◒ Pondering (3s)")
    tui._busy_activity_tick = 4
    now["value"] = 104.0
    assert tui._busy_activity_label().startswith("◐ Pondering (4s)")


def test_busy_activity_label_includes_step_and_turn_tokens(tmp_path, monkeypatch):
    now = {"value": 100.0}
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: now["value"])
    stats = UsageStats()
    stats.total_input_tokens = 10_000
    stats.total_output_tokens = 1_000
    stats.begin_turn()
    stats.total_input_tokens = 126_100
    stats.total_output_tokens = 1_043
    status = SimpleNamespace(workspace=str(tmp_path), usage_stats=stats)
    tui = PureTui(status, COMMANDS)
    tui._busy = True
    tui._busy_started_at = 37.0
    tui._busy_activity_verb = "Pondering"
    dock.record_status("agent:-1:progress", "Agent step 1/100", stage="agent step")

    assert tui._busy_activity_label() == "◐ Pondering (1m 3s step 1/100 ↑116.1k ↓43)"


def test_busy_activity_label_omits_elapsed_without_start_time(tmp_path):
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = None
    tui._busy_activity_verb = "Pondering"

    assert tui._busy_activity_label() == "◐ Pondering"


def test_busy_activity_glyph_cycles_rainbow_styles(tmp_path):
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 0.0
    tui._busy_activity_verb = "Pondering"
    expected_styles = [
        "#E06C75",
        "#D77757",
        "#E5C07B",
        "#98C379",
        "#56D4DD",
        "#61AFEF",
        "#C678DD",
    ]

    for tick, style in enumerate(expected_styles):
        tui._busy_activity_tick = tick
        text = tui._busy_activity_text(80)

        assert style in _styles_covering(text, text.plain[:1])
        assert "#D77757" in _styles_covering(text, " Pondering")


@pytest.mark.asyncio
async def test_busy_activity_verb_randomized_once_per_turn(tmp_path, monkeypatch):
    now = 20.0
    monkeypatch.setattr("voidx.ui.tui.app.time.monotonic", lambda: now)
    monkeypatch.setattr("voidx.ui.tui.app.random.choice", lambda _choices: "Ruminating")
    tui = _tui(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def on_submit(_text: str) -> bool:
        started.set()
        await release.wait()
        return True

    consumer = asyncio.create_task(tui._consume(on_submit))
    try:
        tui._queue.put_nowait("run")
        await asyncio.wait_for(started.wait(), timeout=1)

        assert tui._busy_activity_verb == "Ruminating"

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert tui._busy_activity_verb == ""
    finally:
        tui._queue.put_nowait(None)
        await asyncio.wait_for(consumer, timeout=1)


@pytest.mark.asyncio
async def test_busy_started_at_set_and_cleared_by_consume_loop(tmp_path, monkeypatch):
    now = 10.0
    monkeypatch.setattr("voidx.ui.tui.app.time.monotonic", lambda: now)
    tui = _tui(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def on_submit(_text: str) -> bool:
        started.set()
        await release.wait()
        return True

    consumer = asyncio.create_task(tui._consume(on_submit))
    try:
        tui._queue.put_nowait("run")
        await asyncio.wait_for(started.wait(), timeout=1)

        assert tui._busy_started_at == 10.0

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert tui._busy_started_at is None
    finally:
        tui._queue.put_nowait(None)
        await asyncio.wait_for(consumer, timeout=1)


def test_busy_activity_tick_repaints_bottom_line_with_pinned_todo(tmp_path, monkeypatch):
    now = {"value": 1.0}
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: now["value"])
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )
    tui = _tui(tmp_path)
    tui._running = True
    tui._tty = True
    tui._busy = True
    tui._busy_started_at = 0.0
    tui._busy_activity_verb = "Brewing"
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    dock.begin_capture()
    dock.ensure_agent()
    dock.set_todo_state(
        "0/1 done · 1 active · 0 pending",
        [{"content": "active task", "status": "in_progress"}],
    )

    tui._render_frame()
    initial = _rich_plain(fake_stdout.text)
    assert tui._last_busy_activity_start_row == tui._last_bottom_start_row - 1
    assert "voidx" not in initial
    assert "Todo:" in initial
    assert "Brewing (1s)" in initial
    assert initial.count("Brewing") == 1

    fake_stdout.text = ""
    now["value"] = 2.0
    monkeypatch.setattr(
        tui,
        "_render_frame",
        lambda: (_ for _ in ()).throw(AssertionError("timer must not full-render")),
    )

    assert tui._render_busy_activity_tick() is True
    tick_output = _rich_plain(fake_stdout.text)
    assert "Brewing (2s)" in tick_output
    assert "Todo:" not in tick_output
    assert "active task" not in tick_output
    assert "voidx" not in tick_output
    assert "\x1b[J" not in fake_stdout.text
    assert "\x1b[K" in fake_stdout.text


@pytest.mark.asyncio
async def test_busy_activity_timer_starts_ticks_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.activity.BUSY_ACTIVITY_TICK_SECONDS", 0.01)
    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    started = asyncio.Event()
    release = asyncio.Event()
    ticked = asyncio.Event()
    ticks = []

    monkeypatch.setattr(tui, "invalidate", lambda: None)

    def tick() -> bool:
        ticks.append(tui._busy_activity_tick)
        ticked.set()
        return True

    monkeypatch.setattr(tui, "_render_busy_activity_tick", tick)

    async def on_submit(_text: str) -> bool:
        started.set()
        await release.wait()
        return True

    consumer = asyncio.create_task(tui._consume(on_submit))
    try:
        tui._queue.put_nowait("run")
        await asyncio.wait_for(started.wait(), timeout=1)

        assert tui._busy_activity_timer_task is not None
        await asyncio.wait_for(ticked.wait(), timeout=1)
        assert ticks[0] >= 1

        release.set()
        for _ in range(10):
            await asyncio.sleep(0.01)
            if tui._busy_activity_timer_task is None:
                break

        assert tui._busy_activity_timer_task is None
        assert tui._busy_started_at is None
        assert tui._busy_activity_tick == 0
    finally:
        tui._queue.put_nowait(None)
        await asyncio.wait_for(consumer, timeout=1)


@pytest.mark.asyncio
async def test_invalidate_coalesces_render_until_throttle_window(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._running = True
    calls = {"flush": 0, "render": 0}

    monkeypatch.setattr(tui, "_flush_committed", lambda: calls.__setitem__("flush", calls["flush"] + 1))
    monkeypatch.setattr(tui, "_render_frame", lambda: calls.__setitem__("render", calls["render"] + 1))

    tui.invalidate()
    tui.invalidate()

    assert calls == {"flush": 0, "render": 0}

    await asyncio.sleep(0)

    assert calls == {"flush": 0, "render": 0}

    await asyncio.sleep(0.03)

    assert calls == {"flush": 1, "render": 1}
    assert tui._render_scheduled is False
