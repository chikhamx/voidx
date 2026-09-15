"""Resize boundary: missing snapshot, multi-cycle resize+commit, width change.

Covers the paths where `_visible_history_for_size` must decline to correct
(no reliable baseline) or where several resize/commit cycles interleave.
"""

import pytest

from test_layout_terminal_model import _terminal_tui
from tui_helpers import setup_dock  # noqa: F401
from voidx.presentation.output.dock import dock
from voidx.presentation.output.events.consumers import DockEventConsumer
from voidx.presentation.output.events.schema import SubagentStarted, ToolStarted
from test_resize_history_boundary import _shrink_ghostty

MARKERS = [f"SB-HISTORY-{i:02}" for i in range(35)]


async def _build_full_load(tui, drain):
    dock.begin_capture()
    for marker in MARKERS:
        dock.tree.new_node(parent=dock.tree.root, node_type="message", header=marker, status="done")
    tui._render_frame()
    await drain()
    tui._flush_committed(force=True)
    await drain()
    consumer = DockEventConsumer(dock)
    consumer.handle(SubagentStarted(agent_id=7, subagent_id="sb-agent", name="Prism", description="SB-ACTIVE-AGENT"))
    consumer.handle(ToolStarted(agent_id=0, tool_call_id="sb-wait", label='Wait("Prism")', tool_name="wait", args='"Prism"', raw_args={"name": "Prism"}))
    tui._busy = True
    tui._render_frame()
    await drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_ghostty_shrink_without_snapshot_falls_back_safely(tmp_path, monkeypatch, worker):
    """No applied snapshot (e.g. after clear): correction must not guess
    coordinates. It keeps the current count and the frame cache invalidation
    produces a clean full repaint."""
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        await _build_full_load(tui, drain)
        assert tui._visible_committed_rows > 0

        # Real clear path: request a clear, render once to consume it (this
        # resets the committed baseline and clears the snapshot), then shrink.
        dock.request_clear_screen()
        tui._render_frame()
        await drain()
        # After a real clear the baseline is reset.
        assert tui._visible_committed_rows == 0

        _shrink_ghostty(screen, tui, monkeypatch, 20)
        tui._render_frame()
        await drain()

        # With no committed baseline the correction declines and the repaint
        # must stay coherent: no duplicate dynamic region, cursor in bounds.
        row, col = screen.cursor
        assert 1 <= row <= 20
        assert sum("SB-ACTIVE-AGENT" in row for row in screen.rows) <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_ghostty_resize_commit_resize_cycle(tmp_path, monkeypatch, worker):
    """resize -> repaint -> commit -> resize again: each cycle must recompute
    the boundary from the latest applied frame, not accumulate drift."""
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        await _build_full_load(tui, drain)

        # First shrink.
        _shrink_ghostty(screen, tui, monkeypatch, 20)
        tui._render_frame()
        await drain()
        assert sum("SB-ACTIVE-AGENT" in row for row in screen.rows) == 1

        # A commit lands at the new size.
        dock.tree.new_node(parent=dock.tree.root, node_type="message", header="SB-CYCLE-COMMIT", status="done")
        tui._render_frame()
        await drain()
        tui._flush_committed(force=True)
        await drain()

        # Second shrink.
        _shrink_ghostty(screen, tui, monkeypatch, 16)
        resized_history = screen.history
        tui._render_frame()
        await drain()

        assert sum("SB-ACTIVE-AGENT" in row for row in screen.rows) == 1, screen.rows
        assert sum('Wait("Prism")' in row for row in screen.rows) == 1
        assert screen.history == resized_history
        assert tuple(
            row for row in (*screen.history, *screen.rows) if row.startswith("SB-HISTORY-")
        ) == tuple(MARKERS)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_width_change_does_not_apply_ghostty_correction(tmp_path, monkeypatch, worker):
    """Width change goes through the reflow path; the Ghostty shrink correction
    must not touch it."""
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        await _build_full_load(tui, drain)
        before_visible = tui._visible_committed_rows

        # Width changes: snapshot.terminal_width != new width -> no correction.
        # Directly assert the correction declines for a width-mismatched size.
        corrected = tui._visible_history_for_size(100, 20)
        assert corrected == before_visible
        corrected = tui._visible_history_for_size(60, 20)
        assert corrected == before_visible
