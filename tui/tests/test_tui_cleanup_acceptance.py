"""Cleanup acceptance through the real consumer and physical terminal model."""

import asyncio
import os
import shutil

import pytest

from test_layout_terminal_model import _assert_applied_screen, _terminal_tui
from tui_helpers import setup_dock  # noqa: F401
from voidx.presentation.output.dock import dock
from voidx.presentation.output.events.consumers import DockEventConsumer
from voidx.presentation.output.events.schema import (
    SubagentFinished, SubagentStarted, TodoUpdated, ToolFinished, ToolStarted,
)
from voidx.presentation.tools.clipboard_image import ClipboardImageResult


async def _paint(tui, screen, drain):
    tui._render_frame()
    await drain()
    return _assert_applied_screen(tui, screen)


def _resize(screen, tui, monkeypatch, width, height):
    # Model a non-reflowing resize: retain top-left cells, clip or pad the rest.
    assert not screen.synchronized
    screen._cells = [
        (row[:width] + [" "] * width)[:width]
        for row in screen._cells[:height]
    ]
    screen._cells.extend([[" "] * width for _ in range(height - len(screen._cells))])
    screen.width, screen.height = width, height
    screen._row = min(screen._row, height - 1)
    screen._col = min(screen._col, width - 1)
    screen._top_margin, screen._bottom_margin = 0, height - 1
    screen._wrap_pending = False
    screen._displayed_cells = screen.cells
    tui._console.width, tui._console.height = width, height
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((width, height)))


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_consumer_subagent_todo_tool_chain_small_viewport(tmp_path, monkeypatch, worker):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=10) as (tui, screen, stream, drain):
        dock.begin_capture()
        for index in range(12):
            dock.tree.new_node(parent=dock.tree.root, node_type="message", header=f"CHAIN-HISTORY-{index:02d}", status="done")
        await _paint(tui, screen, drain)
        tui._flush_committed(force=True)
        await drain()
        assert screen.history
        dock.start_turn("CHAIN-QUESTION")
        consumer = DockEventConsumer(dock)
        events = [
            SubagentStarted(agent_id=7, subagent_id="acceptance-agent", name="explore", description="CHAIN-HEADER"),
            TodoUpdated(agent_id=7, items=[{"id": "scan", "content": "CHAIN-TODO", "status": "active"}], summary="CHAIN-OLD-TODO"),
            ToolStarted(agent_id=7, tool_call_id="chain-read", label="CHAIN-READING", tool_name="read", args='file_path="chain.py"', raw_args={"file_path": "chain.py"}),
            ToolFinished(agent_id=7, tool_call_id="chain-read", label="CHAIN-READ", elapsed=0.1, detail="CHAIN-RESULT"),
            TodoUpdated(agent_id=7, items=[{"id": "scan", "content": "CHAIN-TODO", "status": "done"}], summary="CHAIN-DONE-TODO"),
            SubagentFinished(agent_id=7, subagent_id="acceptance-agent", elapsed=0.2, summary="CHAIN-FINISHED"),
        ]
        for index, event in enumerate(events):
            consumer.handle(event)
            tui._flush_committed()
            await drain()
            await _paint(tui, screen, drain)
            visible = "\n".join(screen.rows)
            history = "\n".join(screen.history)
            assert visible.count("CHAIN-HEADER") <= 1, (index, screen.rows)
            assert not any(text in history for text in ("CHAIN-OLD-TODO", "CHAIN-READING", "chain.py", "status")), (index, screen.history)
            if index == 0:
                assert "CHAIN-HEADER" in visible
            if index == 1:
                assert "CHAIN-OLD-TODO" in visible
            if index == 2:
                assert "chain.py" in visible
            if index < len(events) - 1:
                assert "CHAIN-HEADER" not in history, (index, screen.history)
            sentinels = [row for row in (*screen.history, *screen.rows) if row.startswith("CHAIN-HISTORY-")]
            assert sentinels == [f"CHAIN-HISTORY-{i:02d}" for i in range(12)]
        tui._flush_committed(force=True)
        await drain()
        await _paint(tui, screen, drain)
        rows = "\n".join((*screen.history, *screen.rows))
        assert rows.count("CHAIN-HEADER") == 1
        assert rows.count("CHAIN-QUESTION") == 1
        # Child tools are transient progress, not independent transcript nodes.
        assert "chain.py" not in rows, (screen.history, screen.rows)
        assert "CHAIN-FINISHED" in rows
        assert "CHAIN-DONE-TODO" in rows
        assert "CHAIN-READING" not in rows
        assert "CHAIN-OLD-TODO" not in rows


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_choice_and_slash_exit_clean_screen_and_history(tmp_path, monkeypatch, worker):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (tui, screen, stream, drain):
        await _paint(tui, screen, drain)
        history = screen.history
        task = asyncio.create_task(tui.ask_choice("CHOICE-TRANSIENT", [("ACCEPT-TRANSIENT", "yes", "")]))
        try:
            await asyncio.sleep(0)
            await _paint(tui, screen, drain)
            assert "CHOICE-TRANSIENT" in "\n".join(screen.rows)
            tui._handle_escape()
            assert await asyncio.wait_for(task, 2) is None
            await _paint(tui, screen, drain)
            assert "TRANSIENT" not in "\n".join(screen.rows)
            assert screen.history == history
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        tui._process_input(b"/")
        await _paint(tui, screen, drain)
        assert tui._command_panel_active
        panel_rows = screen.rows
        tui._handle_escape()
        await _paint(tui, screen, drain)
        assert not tui._command_panel_active
        assert screen.rows != panel_rows
        assert screen.history == history


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_thinking_growth_shrink_and_resize_preserve_history(tmp_path, monkeypatch, worker):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (tui, screen, stream, drain):
        dock.begin_capture()
        dock.start_turn("RESIZE-QUESTION")
        tui._flush_committed()
        await drain()
        for width, height, count in [(80, 12, 1), (80, 12, 12), (44, 9, 12), (44, 9, 1), (80, 14, 2), (80, 14, 1)]:
            if (width, height) != (screen.width, screen.height):
                _resize(screen, tui, monkeypatch, width, height)
            dock.set_stream("\n".join(f"THOUGHT-{i}" for i in range(count)), phase="thinking")
            await _paint(tui, screen, drain)
            assert "THOUGHT-" in "\n".join(screen.rows)
            assert "THOUGHT-" not in "\n".join(screen.history)
            assert "Thinking" not in "\n".join(screen.history)
            if count == 1:
                assert "THOUGHT-1" not in "\n".join(screen.rows)
        dock.commit_stream()
        tui._flush_committed()
        await drain()
        await _paint(tui, screen, drain)
        rows = "\n".join((*screen.history, *screen.rows))
        assert "THOUGHT-" not in rows
        assert rows.count("RESIZE-QUESTION") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_multiple_attachments_narrow_multiline_cursor(tmp_path, monkeypatch, worker):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (tui, screen, stream, drain):
        _resize(screen, tui, monkeypatch, 28, 12)
        monkeypatch.setattr("voidx_cli.clipboard_mixin._paste_clipboard_image_from_system", lambda workspace: ClipboardImageResult(status="ok", message="Pasted image", rel_path=".voidx/attachments/acceptance.png", size=123))
        tui.paste_clipboard_image()
        tui.paste_clipboard_image()
        tui._process_input(b"\x1b[200~first line\nsecond line\x1b[201~")
        assert len(tui._paste_entries) == 3
        for data in [b"", b"\x01", b"\x05", b"\x1b[D"]:
            if data:
                tui._process_input(data)
            snapshot = await _paint(tui, screen, drain)
            assert snapshot.bottom.input.visual_rows > 1
            sequence, _ = tui._input_cursor_target()
            assert sequence == f"\x1b[{screen.cursor[0]};{screen.cursor[1]}H"
            assert screen.history == ()
        assert "Pasted image" in "\n".join(screen.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_block_gap_and_identical_redraw_physical_rows(tmp_path, monkeypatch, worker):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (tui, screen, stream, drain):
        for label in ["BLOCK-ONE", "BLOCK-TWO"]:
            dock.tree.new_node(parent=dock.tree.root, node_type="assistant", header=label)
        await _paint(tui, screen, drain)
        first, second = (next(i for i, row in enumerate(screen.rows) if label in row) for label in ["BLOCK-ONE", "BLOCK-TWO"])
        assert second == first + 2
        assert screen.rows[first + 1] == ""
        tui._flush_committed(force=True)
        await drain()
        await _paint(tui, screen, drain)
        rows, history = screen.rows, screen.history
        tui._full_frame_repaint_pending = True
        await _paint(tui, screen, drain)
        assert screen.rows == rows
        assert screen.history == history
        all_rows = (*screen.history, *screen.rows)
        first, second = (next(i for i, row in enumerate(all_rows) if label in row) for label in ["BLOCK-ONE", "BLOCK-TWO"])
        assert second == first + 2
        assert all_rows[first + 1] == ""
        assert sum("BLOCK-ONE" in row for row in all_rows) == 1
        assert sum("BLOCK-TWO" in row for row in all_rows) == 1
