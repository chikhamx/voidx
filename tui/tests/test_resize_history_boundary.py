"""Height-only Ghostty resize acceptance with committed history."""

import os
import shutil

import pytest

from test_layout_terminal_model import _assert_applied_screen, _terminal_tui
from tui_helpers import setup_dock  # noqa: F401
from voidx.presentation.output.dock import dock
from voidx.presentation.output.events.consumers import DockEventConsumer
from voidx.presentation.output.events.schema import SubagentStarted, ToolStarted


def _shrink_ghostty(screen, tui, monkeypatch, height):
    removed = screen.height - height
    trimmed = 0
    while trimmed < removed:
        row = screen.height - trimmed - 1
        # Ghostty trimTrailingBlankRows stops only at a row with text (or a
        # tracked pin from selection/saved-cursor, which our TUI never creates).
        # The live cursor row is NOT pinned.
        if any(cell not in (None, " ") for cell in screen._cells[row]):
            break
        trimmed += 1
    pushed = removed - trimmed
    screen.scrollback.extend(tuple(row) for row in screen._cells[:pushed])
    screen._cells = screen._cells[pushed:pushed + height]
    screen._row = max(0, screen._row - pushed)
    screen.height = height
    screen._top_margin, screen._bottom_margin = 0, height - 1
    screen._wrap_pending = False
    screen._displayed_cells = screen.cells
    tui._console.height = height
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, height)))


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("heights", [(20,), (16,), (11,), (20, 16, 11)])
async def test_ghostty_shrink_preserves_history_and_dynamic_boundary(tmp_path, monkeypatch, worker, heights):
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        dock.begin_capture()
        markers = [f"RESIZE-HISTORY-{i:02}" for i in range(35)]
        for marker in markers:
            dock.tree.new_node(parent=dock.tree.root, node_type="message", header=marker, status="done")
        tui._render_frame()
        await drain()
        tui._flush_committed(force=True)
        await drain()
        consumer = DockEventConsumer(dock)
        consumer.handle(SubagentStarted(agent_id=7, subagent_id="resize-agent", name="Prism", description="RESIZE-ACTIVE-AGENT"))
        consumer.handle(ToolStarted(agent_id=7, tool_call_id="resize-read", label="Searching", tool_name="read", args='file_path="probe.py"', raw_args={"file_path": "probe.py"}))
        consumer.handle(ToolStarted(agent_id=0, tool_call_id="resize-wait", label='Wait("Prism")', tool_name="wait", args='"Prism"', raw_args={"name": "Prism"}))
        tui._busy = True
        tui._render_frame()
        await drain()
        snapshot = _assert_applied_screen(tui, screen)
        assert tui._visible_committed_rows > 0
        assert snapshot.frame_start_row + snapshot.frame_rows - 1 == 24
        for height in heights:
            _shrink_ghostty(screen, tui, monkeypatch, height)
            resized_history = screen.history
            tui._render_frame()
            await drain()
            _assert_applied_screen(tui, screen)
            assert sum("RESIZE-ACTIVE-AGENT" in row for row in screen.rows) == 1, screen.rows
            assert sum('Wait("Prism")' in row for row in screen.rows) == 1, screen.rows
            assert screen.history == resized_history
            assert [row for row in (*screen.history, *screen.rows) if row.startswith("RESIZE-HISTORY-")] == markers
            assert all(row.startswith("RESIZE-HISTORY-") for row in screen.rows[:tui._visible_committed_rows])
            tui._render_frame()
            await drain()
            assert screen.history == resized_history


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_ghostty_shrink_trims_unused_tail_before_history(tmp_path, monkeypatch, worker):
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        dock.begin_capture()
        dock.tree.new_node(parent=dock.tree.root, node_type="message", header="SHORT-HISTORY", status="done")
        tui._render_frame()
        await drain()
        tui._flush_committed(force=True)
        await drain()
        dock.tree.new_node(parent=dock.tree.root, node_type="message", header="SHORT-ACTIVE", status="running")
        tui._render_frame()
        await drain()
        before = screen.history
        visible = tui._visible_committed_rows
        _shrink_ghostty(screen, tui, monkeypatch, 20)
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)
        assert tui._visible_committed_rows == visible
        assert screen.history == before
        assert sum("SHORT-HISTORY" in row for row in screen.rows) == 1
        assert sum("SHORT-ACTIVE" in row for row in screen.rows) == 1


def test_resize_history_correction_is_scoped_to_ghostty(tmp_path, monkeypatch):
    from tui_helpers import _tui
    from types import SimpleNamespace

    tui = _tui(tmp_path)
    tui._visible_committed_rows = 14
    tui._prev_frame_lines = ["dynamic"] * 10
    tui._applied_layout_snapshot = SimpleNamespace(
        terminal_width=80, terminal_height=24, frame_start_row=15, cursor_row=22,
    )
    for program in ("iTerm.app", "Apple_Terminal", ""):
        monkeypatch.setenv("TERM_PROGRAM", program)
        assert tui._visible_history_for_size(80, 20) == 14
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    assert tui._visible_history_for_size(79, 20) == 14
    assert tui._visible_history_for_size(80, 25) == 14
    assert tui._visible_history_for_size(80, 20) == 10
    assert tui._visible_committed_rows == 14
