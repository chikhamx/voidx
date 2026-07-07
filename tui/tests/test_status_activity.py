from tui_helpers import *  # noqa: F403

import asyncio
import os
import shutil
import sys
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.style import Style

from voidx.llm.usage import UsageStats
from voidx.ui.commands import COMMANDS
from voidx.ui.output.dock import dock
from voidx_cli import PureTui, _rendered_row_count
from voidx_cli.state import InputState, RenderState


def test_busy_activity_label_rotates_centered_glyphs(tmp_path, monkeypatch):
    now = {"value": 100.0}
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: now["value"])
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
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 105.0)
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




def test_busy_activity_label_moves_retry_delay_into_verb_and_trims_detail(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Pondering"
    dock.begin_capture()
    try:
        dock.record_status(
            "llm:retry",
            "Retrying",
            "retrying in 2s: Connection error.",
            stage="working",
        )

        assert tui._busy_activity_label() == "◐ Retrying in 2s (5s Connection error.)"
    finally:
        dock.deactivate()
        dock.reset()


def test_busy_activity_label_truncates_long_retry_error_detail(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 100.0
    dock.begin_capture()
    try:
        long_error = (
            "Error code: 503 - {'error': {'message': 'Provider failed to respond within "
            "30000ms (cch_session_id=abc123)', 'code': 'service_unavailable_error'}}"
        )
        dock.record_status(
            "llm:retry",
            "Retrying",
            f"retrying in 2s: {long_error}",
            stage="working",
        )

        label = tui._busy_activity_label()
        assert label.startswith("◐ Retrying in 2s (5s Error code: 503")
        assert "retrying in 2s:" not in label
        assert "cch_session_id" not in label
        assert label.endswith("…)")
    finally:
        dock.deactivate()
        dock.reset()


def test_busy_activity_label_prefers_error_over_retry_status(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Pondering"
    dock.begin_capture()
    try:
        dock.record_status(
            "llm:retry",
            "Retrying",
            "retrying in 4s: provider timeout",
            stage="working",
        )
        dock.record_status(
            "error:current",
            "Error",
            "provider timeout",
            stage="error",
        )

        assert tui._busy_activity_label() == "◐ Error (5s provider timeout)"
    finally:
        dock.deactivate()
        dock.reset()


def test_busy_activity_label_prefers_progress_over_retry_status(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Pondering"
    dock.begin_capture()
    try:
        dock.record_status(
            "llm:retry",
            "Retrying",
            "retrying in 4s: provider timeout",
            stage="working",
        )
        dock.record_status("agent:-1:progress", "Agent step 3/10", stage="agent step")

        assert tui._busy_activity_label() == "◐ step 3/10 (5s)"
    finally:
        dock.deactivate()
        dock.reset()


def test_busy_activity_renders_thinking_content_below_verb(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 105.0)
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


def test_busy_activity_renders_permission_details_below_requesting_verb(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=100, height=24, _environ={})
    tui._busy = True
    tui._busy_started_at = 100.0
    tui._busy_activity_verb = "Canoodling"
    dock.begin_capture()
    try:
        long_command = ".venv/bin/python -m pytest tests/test_tools/test_agent.py --very-long-option that-keeps-going"
        dock.record_status(
            "permission:request",
            "Requesting",
            f"1. git\n   target: {long_command}\n   args: commit -m docs",
            stage="permission",
        )

        rendered = _render_lines(tui, width=100)
        lines = [_rich_plain(line) for line in rendered]
        requesting_index = next(i for i, line in enumerate(lines) if "Requesting (5s)" in line)
        detail_index = next(i for i, line in enumerate(lines) if "args: commit -m docs" in line)
        target_index = next(i for i, line in enumerate(lines) if "target:" in line)

        assert requesting_index < detail_index
        assert not any("Canoodling" in line for line in lines)
        assert lines[target_index].endswith("…")
        detail_elements = tui._render_busy_activity_elements(100)[1:]
        verb_element = tui._render_busy_activity_elements(100)[0]
        target_detail = next(text for text in detail_elements if "target:" in text.plain)
        args_detail = next(text for text in detail_elements if "args: commit -m docs" in text.plain)
        verb_style = Style.parse(_styles_covering(verb_element, "Requesting")[0])
        for text in (target_detail, args_detail):
            assert cell_len(text.plain) == 100
            style = _base_style(text)
            assert style.color != verb_style.color
            assert style.bgcolor == Style.parse("on #3a3937").bgcolor

        dock.clear_status_record("permission:request")

        lines = [_rich_plain(line) for line in _render_lines(tui, width=100)]
        assert any("Canoodling (5s)" in line for line in lines)
        assert not any("args: commit -m docs" in line for line in lines)
    finally:
        dock.deactivate()
        dock.reset()


def test_busy_activity_compacts_many_permission_details(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=100, height=24, _environ={})
    tui._busy = True
    tui._busy_started_at = 100.0
    dock.begin_capture()
    try:
        detail = "\n".join(
            [
                "1. replace",
                "   target: docs/specs/llm-error-optimization-2026-07-05.md",
                "   file_path: docs/specs/llm-error-optimization-2026-07-05.md",
                "   bounds: [{'line_no': 234, 'anchor': '| `clarify.py:72` | user skipped'}]",
                "   new_string: | `clarify.py:72` | user skipped | `summary=clarify: skipped` |",
                "2. replace",
                "   target: docs/specs/llm-error-optimization-2026-07-05.md",
                "   file_path: docs/specs/llm-error-optimization-2026-07-05.md",
                "   bounds: [{'line_no': 291, 'anchor': '| `tests/test_tools/test_plan_checkpoint.py` |'}]",
                "   new_string: | `tests/test_tools/test_plan_checkpoint.py` | 新增 summary 测试 |",
                "3. replace",
                "   target: docs/specs/llm-error-optimization-2026-07-05.md",
                "   file_path: docs/specs/llm-error-optimization-2026-07-05.md",
                "   bounds: [{'line_no': 334, 'anchor': './python.py -m pytest tests/test_tools/test_plan_checkpoint.py'}]",
                "   new_string: ./python.py -m pytest tests/test_tools/test_plan_checkpoint.py tests/test_tools/test_basic.py",
            ]
        )
        dock.record_status("permission:request", "Requesting", detail, stage="permission")

        detail_elements = tui._render_busy_activity_elements(100)[1:]
        detail_lines = [text.plain.rstrip() for text in detail_elements]

        assert len(detail_lines) <= 5
        assert detail_lines == [
            "1. replace -> docs/specs/llm-error-optimization-2026-07-05.md",
            "2. replace -> docs/specs/llm-error-optimization-2026-07-05.md",
            "3. replace -> docs/specs/llm-error-optimization-2026-07-05.md",
        ]
        assert not any("bounds:" in line for line in detail_lines)
        assert not any("new_string:" in line for line in detail_lines)
    finally:
        dock.deactivate()
        dock.reset()


def test_busy_activity_label_includes_step_and_turn_tokens(tmp_path, monkeypatch):
    now = {"value": 100.0}
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: now["value"])
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
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 123.0)
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
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 124.0)
    tui = _tui(tmp_path)
    tui._busy = True
    tui._busy_started_at = 120.0
    tui._busy_activity_verb = "Pondering"
    dock.record_status(
        "compaction",
        "Compacting",
        "summarizing old messages",
        stage="compacting",
    )

    assert tui._busy_activity_label() == "◐ Compacting (4s summarizing old messages)"

    dock.clear_status_record("compaction")

    assert tui._busy_activity_label() == "◐ Pondering (4s)"


def test_busy_activity_label_places_compacting_detail_last(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 124.0)
    tui = _tui(tmp_path)
    tui.status.latest_action = lambda: "reading"
    tui._busy = True
    tui._busy_started_at = 120.0
    tui._busy_activity_verb = "Pondering"
    dock.record_status(
        "compaction",
        "Compacting",
        "summarizing 118 old messages",
        stage="compacting",
    )

    assert tui._busy_activity_label() == "◐ Compacting (4s →reading summarizing 118 old messages)"


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
    monkeypatch.setattr("voidx_cli.app.time.monotonic", lambda: now)
    monkeypatch.setattr("voidx_cli.app.random.choice", lambda _choices: "Ruminating")
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
    monkeypatch.setattr("voidx_cli.app.time.monotonic", lambda: now)
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
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: now["value"])
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
        [{"content": "active task", "status": "active"}],
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
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: now["value"])
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
    monkeypatch.setattr("voidx_cli.activity.BUSY_ACTIVITY_TICK_SECONDS", 0.01)
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
async def test_busy_activity_timer_full_renders_when_tick_region_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx_cli.activity.BUSY_ACTIVITY_TICK_SECONDS", 0.01)
    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._busy = True
    rendered = asyncio.Event()
    calls = {"tick": 0, "render": 0}

    def tick() -> bool:
        calls["tick"] += 1
        tui._busy = False
        return False

    def render_frame() -> None:
        calls["render"] += 1
        rendered.set()

    monkeypatch.setattr(tui, "_render_busy_activity_tick", tick)
    monkeypatch.setattr(tui, "_render_frame", render_frame)

    await tui._busy_activity_timer()

    assert calls == {"tick": 1, "render": 1}
    assert rendered.is_set()


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


def _base_style(text: "Text") -> Style:
    style = text.style
    if isinstance(style, Style):
        return style
    return Style.parse(str(style))
