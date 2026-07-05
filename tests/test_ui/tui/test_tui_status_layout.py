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
            {"content": "inspect behavior", "status": "active"},
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
        [{"content": "active task", "status": "active"}],
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
        [{"content": "active task", "status": "active"}],
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


