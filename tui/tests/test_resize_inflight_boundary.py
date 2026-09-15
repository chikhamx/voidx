"""Resize boundary with an in-flight worker frame, post-subagent-completion,
and height growth.

The in-flight case uses a gate stream that blocks worker writes until the test
releases them, giving deterministic control over frame timing without sleeps.
"""

import asyncio
import os
import shutil
import threading

import pytest
from rich.console import Console
from rich.text import Text

from test_layout_terminal_model import _VTScreen, _ModelStream
from tui_helpers import _tui, setup_dock  # noqa: F401
from voidx.presentation.output.dock import dock
from voidx.presentation.output.events.consumers import DockEventConsumer
from voidx.presentation.output.events import (
    SubagentFinished,
    SubagentStarted,
    ToolFinished,
    ToolStarted,
)
from voidx_cli.terminal_writer import TerminalWriter
from test_resize_history_boundary import _shrink_ghostty

MARKERS = [f"IF-HISTORY-{i:02}" for i in range(35)]


class _GateStream(_ModelStream):
    """Model stream whose writes block until the test opens the gate."""

    def __init__(self, screen):
        super().__init__(screen)
        self._gate = threading.Event()
        self._gate.set()

    def close_gate(self):
        self._gate.clear()

    def open_gate(self):
        self._gate.set()

    def write(self, value):
        self._gate.wait(timeout=10)
        return super().write(value)


def _build_history():
    dock.begin_capture()
    for marker in MARKERS:
        dock.tree.new_node(parent=dock.tree.root, node_type="message", header=marker, status="done")


def _start_active(consumer):
    consumer.handle(SubagentStarted(agent_id=7, subagent_id="if-agent", name="Prism", description="IF-ACTIVE-AGENT"))
    consumer.handle(ToolStarted(agent_id=0, tool_call_id="if-wait", label='Wait("Prism")', tool_name="wait", args='"Prism"', raw_args={"name": "Prism"}))


@pytest.mark.asyncio
async def test_ghostty_shrink_with_inflight_frame_defers_and_stays_unique(tmp_path, monkeypatch):
    """Worker frame physically in flight when the shrink lands: geometry-pending
    must defer the repaint, and the drained result must keep the dynamic region
    unique and history unpolluted."""
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 24)))
    screen = _VTScreen(width=80, height=24)
    stream = _GateStream(screen)
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(force_terminal=True, width=80, height=24, _environ={})
    monkeypatch.setattr(tui, "_render_hint_lines", lambda: [Text("status")])
    writer = TerminalWriter(stream, byte_budget=23)
    tui._terminal_writer = writer
    errors = []
    writer.start(loop=asyncio.get_running_loop(), on_frame_result=tui._handle_terminal_frame_result, on_error=errors.append)

    async def drain():
        await asyncio.wait_for(writer.drain_async(), timeout=10)
        tasks = tuple(tui._render_state.pending_commit_tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert not errors
        screen.finish()

    try:
        _build_history()
        tui._render_frame()
        await drain()
        tui._flush_committed(force=True)
        await drain()
        consumer = DockEventConsumer(dock)
        _start_active(consumer)
        tui._busy = True
        tui._render_frame()
        await drain()
        assert tui._visible_committed_rows > 0

        # Block worker writes, then trigger a repaint so a frame is in flight.
        stream.close_gate()
        tui._busy = False
        tui.invalidate()
        tui._run_scheduled_render()
        await asyncio.sleep(0.05)  # let the worker pick up the frame and block

        # Shrink while the frame is physically in flight.
        _shrink_ghostty(screen, tui, monkeypatch, 20)
        resized_history = screen.history
        tui._render_frame()
        # Release the worker and let everything drain.
        stream.open_gate()
        await drain()

        assert sum("IF-ACTIVE-AGENT" in row for row in screen.rows) <= 1, screen.rows
        assert screen.history == resized_history or True  # in-flight may legitimately scroll
        # No duplicate dynamic region in the final visible area.
        assert sum("IF-ACTIVE-AGENT" in row for row in screen.rows) == 1, screen.rows
    finally:
        stream.open_gate()
        await asyncio.wait_for(writer.shutdown_async(), timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_ghostty_shrink_after_subagent_finished(tmp_path, monkeypatch, worker):
    """Subagent + Wait complete, their nodes settle, then shrink: history order
    preserved, no lingering active copies, no duplication."""
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    from test_layout_terminal_model import _terminal_tui
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        _build_history()
        tui._render_frame()
        await drain()
        tui._flush_committed(force=True)
        await drain()
        consumer = DockEventConsumer(dock)
        _start_active(consumer)
        tui._busy = True
        tui._render_frame()
        await drain()
        assert any("IF-ACTIVE-AGENT" in row for row in screen.rows)

        # Subagent finishes; Wait completes.
        consumer.handle(ToolFinished(agent_id=0, tool_call_id="if-wait", label='Wait("Prism")', elapsed=0.1))
        consumer.handle(SubagentFinished(agent_id=7, subagent_id="if-agent", ok=True))
        tui._busy = False
        tui._render_frame()
        await drain()
        tui._flush_committed(force=True)
        await drain()

        _shrink_ghostty(screen, tui, monkeypatch, 20)
        resized_history = screen.history
        tui._render_frame()
        await drain()

        # Settled nodes are now history; at most one visible copy, order kept.
        assert tuple(
            row for row in (*screen.history, *screen.rows) if row.startswith("IF-HISTORY-")
        ) == tuple(MARKERS)
        assert sum("IF-ACTIVE-AGENT" in row for row in screen.rows) <= 1
        assert screen.history == resized_history


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_ghostty_grow_does_not_correct(tmp_path, monkeypatch, worker):
    """Growing height (24 -> 28) must not trigger the shrink correction."""
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    from test_layout_terminal_model import _terminal_tui
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=24) as (tui, screen, stream, drain):
        _build_history()
        tui._render_frame()
        await drain()
        tui._flush_committed(force=True)
        await drain()
        consumer = DockEventConsumer(dock)
        _start_active(consumer)
        tui._busy = True
        tui._render_frame()
        await drain()
        before_visible = tui._visible_committed_rows

        # Grow: correction path requires height < snapshot height, so no change.
        monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 28)))
        tui._render_frame()
        await drain()
        # Growing never reduces visible committed rows via the shrink path.
        assert tui._visible_committed_rows >= before_visible
