"""Resize boundary when a commit interleaves before the shrink.

A commit can land while the dynamic region is live. These tests pin that the
Ghostty correction still holds when the shrink happens right after a commit.
"""

import pytest

from test_layout_terminal_model import _terminal_tui
from tui_helpers import setup_dock  # noqa: F401
from voidx.presentation.output.dock import dock
from voidx.presentation.output.events.consumers import DockEventConsumer
from voidx.presentation.output.events.schema import SubagentStarted, ToolStarted
from test_resize_history_boundary import _shrink_ghostty

MARKERS = [f"CR-HISTORY-{i:02}" for i in range(35)]


async def _build_full_load(tui, drain):
    dock.begin_capture()
    for marker in MARKERS:
        dock.tree.new_node(parent=dock.tree.root, node_type="message", header=marker, status="done")
    tui._render_frame()
    await drain()
    tui._flush_committed(force=True)
    await drain()
    consumer = DockEventConsumer(dock)
    consumer.handle(SubagentStarted(agent_id=7, subagent_id="cr-agent", name="Prism", description="CR-ACTIVE-AGENT"))
    consumer.handle(ToolStarted(agent_id=0, tool_call_id="cr-wait", label='Wait("Prism")', tool_name="wait", args='"Prism"', raw_args={"name": "Prism"}))
    tui._busy = True
    tui._render_frame()
    await drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_ghostty_shrink_after_commit_keeps_dynamic_boundary(tmp_path, monkeypatch, worker):
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        await _build_full_load(tui, drain)
        assert tui._visible_committed_rows > 0

        # A commit lands while the dynamic region is live, then shrink.
        dock.tree.new_node(parent=dock.tree.root, node_type="message", header="CR-LATE-COMMIT", status="done")
        tui._render_frame()
        await drain()
        tui._flush_committed(force=True)
        await drain()

        _shrink_ghostty(screen, tui, monkeypatch, 20)
        resized_history = screen.history
        tui._render_frame()
        await drain()

        assert sum("CR-ACTIVE-AGENT" in row for row in screen.rows) == 1, screen.rows
        assert sum('Wait("Prism")' in row for row in screen.rows) == 1, screen.rows
        assert screen.history == resized_history


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_ghostty_consecutive_shrink_without_repaint(tmp_path, monkeypatch, worker):
    """Two shrinks land before any repaint: the second model rearrangement must
    compose with the first, and the single repaint must respect the final size."""
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        await _build_full_load(tui, drain)
        assert tui._visible_committed_rows > 0

        # 24 -> 20 -> 16 with no repaint in between.
        _shrink_ghostty(screen, tui, monkeypatch, 20)
        _shrink_ghostty(screen, tui, monkeypatch, 16)
        resized_history = screen.history
        tui._render_frame()
        await drain()

        assert sum("CR-ACTIVE-AGENT" in row for row in screen.rows) == 1, screen.rows
        assert sum('Wait("Prism")' in row for row in screen.rows) == 1, screen.rows
        assert screen.history == resized_history
        assert tuple(
            row for row in (*screen.history, *screen.rows) if row.startswith("CR-HISTORY-")
        ) == tuple(MARKERS)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_ghostty_shrink_short_history_not_full(tmp_path, monkeypatch, worker):
    """History does not fill the window: the terminal trims text-free tail rows
    first, so visible history must be preserved without extra scrolling."""
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        dock.begin_capture()
        dock.tree.new_node(parent=dock.tree.root, node_type="message", header="CR-SHORT-HISTORY", status="done")
        tui._render_frame()
        await drain()
        tui._flush_committed(force=True)
        await drain()
        dock.tree.new_node(parent=dock.tree.root, node_type="message", header="CR-SHORT-ACTIVE", status="running")
        tui._render_frame()
        await drain()
        before_history = screen.history
        before_visible = tui._visible_committed_rows

        _shrink_ghostty(screen, tui, monkeypatch, 20)
        tui._render_frame()
        await drain()

        assert tui._visible_committed_rows == before_visible
        assert screen.history == before_history
        assert sum("CR-SHORT-HISTORY" in row for row in screen.rows) == 1
        assert sum("CR-SHORT-ACTIVE" in row for row in screen.rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("height", [4, 2, 1])
async def test_ghostty_shrink_tiny_height_stays_in_bounds(tmp_path, monkeypatch, worker, height):
    """Extreme shrink: bounded projection must keep coordinates valid and the
    cursor on screen; no dynamic duplication, no coordinate overflow."""
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        await _build_full_load(tui, drain)

        _shrink_ghostty(screen, tui, monkeypatch, height)
        resized_history = screen.history
        tui._render_frame()
        await drain()

        # All rows are within the new viewport; cursor is on screen.
        assert len(screen.rows) == height
        row, col = screen.cursor
        assert 1 <= row <= height
        assert 1 <= col <= 80
        # Committed history order preserved; no duplication introduced.
        assert tuple(
            r for r in (*screen.history, *screen.rows) if r.startswith("CR-HISTORY-")
        ) == tuple(MARKERS)
