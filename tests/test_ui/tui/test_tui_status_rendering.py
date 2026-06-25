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


def test_todo_busy_and_choice_panel_render_once_in_full_frame(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Working"
    tui._input_lines = ["review change"]
    tui._cursor_col = len("review change")
    tui._active_choice = [("review", "review", ""), ("implement", "implement", "")]
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
    tui._active_choice = [("review", "review", ""), ("implement", "implement", "")]
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


def test_busy_activity_label_replaces_verb_during_thinking_stream(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Pondering"
    dock.begin_capture()
    try:
        dock.start_turn("trace thinking")
        dock.set_stream("checking transient status removal", phase="thinking")

        assert tui._busy_activity_label() == "◐ Thinking (5s)"

        dock.commit_stream(refresh=False)
        assert tui._busy_activity_label() == "◐ Pondering (5s)"
    finally:
        dock.deactivate()
        dock.reset()


def test_busy_activity_renders_thinking_content_below_verb(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=100, height=24, _environ={})
    tui._busy = True
    tui._busy_started_at = 100.0
    dock.begin_capture()
    try:
        dock.start_turn("trace thinking")
        dock.set_stream("checking transient status removal", phase="thinking")

        lines = [_rich_plain(line) for line in _render_lines(tui, width=100)]
        thinking_index = next(i for i, line in enumerate(lines) if "Thinking (5s)" in line)
        content_index = next(i for i, line in enumerate(lines) if "checking transient" in line)

        assert thinking_index < content_index
    finally:
        dock.deactivate()
        dock.reset()


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

    assert tui._busy_activity_label() == "◐ step 1/100 (1m 3s ↑116.1k ↓43)"


def test_busy_activity_label_includes_active_analyzing_status(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: 123.0)
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 120.0
    tui._busy_activity_verb = "Pondering"
    dock.record_status(
        "turn:analyzing",
        "Analyzing",
        "loading session and preparing context",
        stage="analyzing",
    )

    assert tui._busy_activity_label() == "◐ Analyzing (3s)"

    dock.clear_status_record("turn:analyzing")

    assert tui._busy_activity_label() == "◐ Pondering (3s)"


def test_busy_activity_label_includes_active_compacting_status(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: 124.0)
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 120.0
    tui._busy_activity_verb = "Pondering"
    dock.record_status(
        "compaction",
        "Compacting context",
        "summarizing old messages",
        stage="compacting",
    )

    assert tui._busy_activity_label() == "◐ Compacting context (4s)"

    dock.clear_status_record("compaction")

    assert tui._busy_activity_label() == "◐ Pondering (4s)"


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


def test_busy_activity_tick_repaints_only_busy_line_above_thinking_content(tmp_path, monkeypatch):
    now = {"value": 1.0}
    monkeypatch.setattr("voidx.ui.tui.render_activity.time.monotonic", lambda: now["value"])
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((100, 12)),
    )
    tui = _tui(tmp_path)
    tui._running = True
    tui._tty = True
    tui._busy = True
    tui._busy_started_at = 0.0
    tui._console = Console(file=None, force_terminal=True, width=100, height=12, _environ={})
    dock.begin_capture()
    try:
        dock.start_turn("trace thinking")
        dock.set_stream("checking transient status removal", phase="thinking")

        tui._render_frame()

        assert tui._last_busy_activity_start_row < tui._last_bottom_start_row - 1

        fake_stdout.text = ""
        now["value"] = 2.0
        monkeypatch.setattr(
            tui,
            "_render_frame",
            lambda: (_ for _ in ()).throw(AssertionError("timer must not full-render")),
        )

        assert tui._render_busy_activity_tick() is True
        tick_output = _rich_plain(fake_stdout.text)
        assert "Thinking (2s)" in tick_output
        assert "checking transient" not in tick_output
        assert "─" not in tick_output
    finally:
        dock.deactivate()
        dock.reset()


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
