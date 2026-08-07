"""Test: guidance turn does not overlay subsequent tool/assistant output.

Root cause: append_guidance_turn adds the guidance turn to root as a settled
node but does not reset _current_agent. When a new stream starts afterwards,
ensure_agent() reuses the old agent node, placing the new stream content
*inside* the old agent node — which renders *before* the guidance turn in the
tree. Since the guidance turn was already flushed to scrollback, the
_committed_line_count now exceeds the stream content's line numbers, causing
the active frame to skip the stream content entirely.
"""
import os
import shutil
import sys

from tui_helpers import _tui, _FakeStdout

from rich.console import Console

from voidx.presentation.output.dock import set_dock, BottomInputDock


def test_guidance_after_tool_does_not_overlay_stream(tmp_path, monkeypatch):
    """Guidance committed after tool execution must not overlay subsequent stream."""
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil, "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    d = BottomInputDock()
    set_dock(d)

    tui = _tui(tmp_path)
    tui._tty = True
    tui._busy = True
    tui._was_busy = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    d.begin_capture()

    # 1. Turn + first stream + commit + tool
    d.start_turn("do something")
    d.set_stream("first assistant response")
    d.commit_stream(refresh=False)
    tool = d.start_tool(
        "Reading",
        'file_path="x.py"',
        tool_name="read",
        raw_args={"file_path": "x.py"},
    )
    d.finish_tool_node(tool, "Read", 0.1, True)
    tui._flush_committed(force=True)

    # 2. Guidance committed (no start_turn in between)
    d.append_guidance_turn("use TypeScript")
    tui._flush_committed()

    # 3. New assistant stream
    d.set_stream("second assistant response")
    tui._flush_committed()

    # 4. Active lines must include stream content
    tree_lines = d.tree.render(80)
    active_lines = tree_lines[tui._committed_line_count:]
    assert any("second assistant response" in line for line in active_lines), \
        f"Stream content missing from active lines (committed={tui._committed_line_count}, " \
        f"total={len(tree_lines)}): {active_lines}"
