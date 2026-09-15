from tui_helpers import *  # noqa: F403

import os
import re
import shutil
import sys

import asyncio
import pytest

from rich.console import Console
from rich.text import Text

from voidx.presentation.output.dock import dock

def test_render_frame_for_commit_returns_frame_at_committed_geometry(
    tmp_path, monkeypatch
):
    """After a commit, the recovery frame must be projected at the committed
    geometry (geometry.next_row) and contain the current vibe line content."""
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(force_terminal=True, width=80, height=24, _environ={})
    tui._running = True

    dock.begin_capture()
    dock.start_turn("task")
    tui._busy = True
    tui._busy_started_at = 0.0
    tui._busy_activity_verb = "Cogitating"

    # Simulate a commit geometry: the frame should restart at row 10.
    from voidx_cli.commit_geometry import CommitGeometry
    geometry = CommitGeometry(visible_rows=9, next_row=10, remaining_frame_rows=5)

    batch = tui._render_frame_for_commit(
        geometry,
        width=tui._frame_width(),
        term_height=24,
    )
    assert batch is not None
    assert batch.start_row == 10
    # The frame must contain the vibe line (busy activity) content.
    plain = "\n".join(batch.target_lines)
    assert "Cogitating" in plain
    # The frame must contain the bottom dock (input border).
    assert "─" in plain


def test_render_frame_uses_absolute_positioning_to_avoid_scrollback_pollution(
    tmp_path, monkeypatch
):
    """Each frame render MUST use absolute cursor positioning so the
    terminal does not scroll while writing the frame.  Relative
    positioning (\x1b[{N}A) pushes old frames into scrollback because
    when the frame grows, \n at the last line triggers a scroll."""

    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil, "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=30, _environ={})

    tui._render_frame()

    text = fake_stdout.text
    # The first cursor movement (before \x1b[J, the clear-screen) MUST
    # be absolute positioning: \x1b[{row};{col}H
    clear_pos = text.find("\x1b[J")
    assert clear_pos > 0, f"Expected \\x1b[J, got: {text!r}"

    before_clear = text[:clear_pos]
    assert re.search(r"\x1b\[\d+;\d+H", before_clear), (
        f"Expected absolute \\x1b[{{row}};{{col}}H before \\x1b[J,"
        f" got: {before_clear!r}"
    )

    match = re.search(r"\x1b\[(\d+);(\d+)H", before_clear)
    row = int(match.group(1))
    # When content fits the terminal, the frame starts at row 1 (top-aligned).
    # When content exceeds terminal height, it anchors near the bottom.
    assert row >= 1, f"Frame start row {row} is invalid"
    assert row <= 30, f"Frame start row {row} exceeds terminal height 30"


def test_render_frame_diff_updates_stream_without_clearing_entire_frame(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 20)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=20, _environ={})
    dock.begin_capture()
    dock.set_stream("first line")

    tui._render_frame()
    assert "\x1b[J" in fake_stdout.text

    fake_stdout.text = ""
    dock.set_stream("first line\nsecond line")
    tui._render_frame()

    assert "\x1b[J" not in fake_stdout.text
    assert "\x1b[K" in fake_stdout.text
    assert "second line" in fake_stdout.text


def test_render_frame_starts_below_short_committed_history(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=30, _environ={})
    for index in range(3):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"committed line {index}",
            collapsed=False,
        )
    tui._committed_line_count = 3
    tui._visible_committed_rows = 3

    tui._render_frame()

    assert tui._last_frame_start_row == 4
    assert fake_stdout.text.startswith("\x1b[?2026h\x1b[4;1H\x1b[J")
    assert fake_stdout.text.endswith("\x1b[?2026l")


def test_render_frame_scrolls_visible_committed_history_before_overlap(
    tmp_path, monkeypatch
):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    for index in range(3):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"committed line {index}",
            collapsed=False,
        )
    for index in range(7):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"active line {index}",
            collapsed=False,
        )
    tui._committed_line_count = 3
    tui._visible_committed_rows = 3

    tui._render_frame()

    if sys.platform == "win32":
        # Rich on Windows wraps full-width separator lines (─*width at
        # capture_width), adding 1 row per separator.  This shifts the
        # scroll math but the scrolling behaviour itself is correct.
        assert tui._last_frame_start_row == 1
        assert tui._visible_committed_rows == 0
    else:
        assert tui._last_frame_start_row == 3
        assert tui._visible_committed_rows == 2
        clear_pos = fake_stdout.text.find("\x1b[J")
        assert fake_stdout.text[:clear_pos] == "\x1b[?2026h\x1b[12;1H\n\x1b[3;1H"


def test_flush_committed_does_not_pad_short_history_to_bottom(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    for index in range(3):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"committed line {index}",
            collapsed=False,
        )

    tui._flush_committed(force=True)

    assert tui._committed_line_count == 3
    assert tui._visible_committed_rows == 3
    assert fake_stdout.text.count("\n") < 10


def test_flush_committed_counts_trailing_blank_separator_row(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("hello")
    dock.set_stream("let me check")

    tui._flush_committed()

    assert tui._committed_line_count == 2
    assert tui._visible_committed_rows == 2




def test_committed_user_separator_is_not_repeated_before_active_assistant(
    tmp_path, monkeypatch
):
    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(file=None, force_terminal=False, width=80, height=24, _environ={})
    dock.begin_capture()
    try:
        dock.start_turn("user message")
        dock.set_stream("assistant message")
        tui._flush_committed()
        tui._render_impl(height=24, capture_plan=True)

        logical = tui._render_plan.logical_plan
        assert logical is not None
        transcript = logical.source_regions[0]
        assert transcript.visual_rows == 1
        assert all(row.strip() for row in transcript.rows)
    finally:
        dock.deactivate()
        dock.reset()

def test_pending_assistant_block_does_not_flush_internal_separator(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("hello")
    dock.set_stream("first assistant")
    dock.commit_stream(refresh=False)
    tool = dock.start_tool(
        "Reading",
        'file_path="x.py"',
        tool_name="read",
        raw_args={"file_path": "x.py"},
    )
    dock.finish_tool_node(tool, "Read", 0.1, True)
    tui._flush_committed(force=True)
    committed_line_count = tui._committed_line_count
    visible_committed_rows = tui._visible_committed_rows
    fake_stdout.text = ""

    dock.set_stream("second assistant")
    tui._flush_committed()

    assert tui._committed_line_count == committed_line_count
    assert tui._visible_committed_rows == visible_committed_rows
    assert fake_stdout.text == ""


def test_pending_assistant_after_assistant_does_not_flush_internal_separator(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("hello")
    dock.set_stream("first assistant")
    dock.commit_stream(refresh=False)
    tui._flush_committed(force=True)
    committed_line_count = tui._committed_line_count
    visible_committed_rows = tui._visible_committed_rows
    fake_stdout.text = ""

    dock.set_stream("second assistant")
    tui._flush_committed()

    assert tui._committed_line_count == committed_line_count
    assert tui._visible_committed_rows == visible_committed_rows
    assert fake_stdout.text == ""


def test_render_after_final_flush_does_not_redraw_flushed_final_answer(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._busy = False
    tui._was_busy = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("1和2的建议简单改吗")
    dock.append_message("改好了，17 个测试全过。segment -> wrapped_line，一行的事。")
    dock.append_message("[dim]✻  37s[/dim]  [dim]·[/dim]  [cyan]3[/cyan] [dim]calls[/dim]", markup=True)

    tui._flush_committed()
    flushed_output = fake_stdout.text
    fake_stdout.text = ""

    tui._render_frame()

    assert "改好了，17 个测试全过" in flushed_output
    assert "改好了，17 个测试全过" not in fake_stdout.text


def test_final_flush_erases_written_row_tails_without_clearing_bottom(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._busy = True
    tui._was_busy = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("1和2的建议简单改吗")
    dock.append_message("改好了，17 个测试全过。segment -> wrapped_line，一行的事。")

    tui._render_frame()
    fake_stdout.text = ""
    tui._busy = False

    tui._flush_committed()

    answer_index = fake_stdout.text.find("改好了")
    assert answer_index != -1
    assert fake_stdout.text.find("\x1b[K", answer_index) > answer_index
    assert "\x1b[J" not in fake_stdout.text
    assert "\n" not in fake_stdout.text


def test_final_flush_invalidates_previous_frame_cache(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._busy = True
    tui._was_busy = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("demo")
    dock.set_stream("当前文档已在仓库里了。", phase="thinking")

    tui._render_frame()
    assert tui._prev_frame_lines is not None

    dock.set_stream("当前文档已在仓库里了。", phase="text")
    dock.commit_stream(refresh=False)
    tui._busy = False

    tui._flush_committed()

    assert tui._prev_frame_lines is None


def test_busy_end_flush_does_not_commit_unsettled_search_or_thinking(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._busy = True
    tui._was_busy = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("find compaction summary")
    tool = dock.start_tool(
        "Searching",
        'pattern="_compaction_summary"',
        tool_name="search",
        tool_call_id="search-1",
        raw_args={"pattern": "_compaction_summary"},
    )

    tui._flush_committed()
    committed = tui._committed_line_count
    assert "Search" not in Text.from_ansi(fake_stdout.text).plain
    assert committed < len(dock.tree.render(tui._frame_width()))

    tui._busy = False
    fake_stdout.text = ""
    tui._flush_committed()

    assert "Search" not in Text.from_ansi(fake_stdout.text).plain
    assert tui._committed_line_count == committed

    dock.finish_tool_node(tool, "Search", 0.1, True, "0 matches")
    fake_stdout.text = ""
    tui._flush_committed()

    flushed = Text.from_ansi(fake_stdout.text).plain
    assert flushed.count('Search("_compaction_summary")') == 1
    assert flushed.count("0 matches") == 1


def test_thinking_only_busy_end_flush_does_not_leave_blank_placeholder(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._busy = True
    tui._was_busy = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("blank thinking question")
    dock.set_stream("checking permissions", phase="thinking")

    tui._flush_committed()
    committed_before = tui._committed_line_count
    assert "checking permissions" not in Text.from_ansi(fake_stdout.text).plain

    tui._busy = False
    fake_stdout.text = ""
    tui._flush_committed()

    flushed = Text.from_ansi(fake_stdout.text).plain
    assert "checking permissions" not in flushed
    assert tui._committed_line_count == committed_before

    dock.commit_stream()
    fake_stdout.text = ""
    tui._flush_committed()

    flushed = Text.from_ansi(fake_stdout.text).plain
    rendered = [_rich_plain(line) for line in dock.tree.render(tui._frame_width())]
    thinking_nodes = [
        node
        for parent in dock.tree.root.children
        for node in [parent, *parent.children]
        if node.node_type == "assistant" and node.payload.get("phase") == "thinking"
    ]

    assert tui._committed_line_count == committed_before
    assert "checking permissions" not in flushed
    assert thinking_nodes == []
    assert rendered.count("") <= 1


def test_force_flush_does_not_commit_unsettled_search(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("force search question")
    dock.start_tool(
        "Searching",
        'pattern="_compaction_summary"',
        tool_name="search",
        tool_call_id="search-force-1",
        raw_args={"pattern": "_compaction_summary"},
    )

    tui._flush_committed(force=True)

    output = Text.from_ansi(fake_stdout.text).plain
    assert "Search" not in output
    assert tui._committed_line_count < len(dock.tree.render(tui._frame_width()))


def test_force_flush_does_not_commit_unsettled_thinking(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("force thinking question")
    dock.set_stream("checking permissions", phase="thinking")

    tui._flush_committed(force=True)

    output = Text.from_ansi(fake_stdout.text).plain
    assert "checking permissions" not in output
    assert tui._committed_line_count < len(dock.tree.render(tui._frame_width()))


@pytest.mark.parametrize("prompt", ["clarify", "checkpoint", "goal_spec"])
def test_force_flush_does_not_commit_interactive_prompt(tmp_path, monkeypatch, prompt):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("force prompt question")
    if prompt == "clarify":
        dock.show_clarify("force-cl-1", "Which approach?", ["implement"])
        marker = "voidx clarify"
    elif prompt == "checkpoint":
        dock.show_checkpoint(
            "force-cp-1",
            {"goal": "Implement the plan", "steps": ["Write the code"]},
            [],
        )
        marker = "voidx plan"
    else:
        dock.show_goal_spec(
            "force-gs-1",
            {"objective": "Implement the plan"},
            [],
        )
        marker = "goal spec"

    tui._flush_committed(force=True)

    output = Text.from_ansi(fake_stdout.text).plain
    assert marker not in output


def test_flush_does_not_replay_committed_lines_after_transient_status_removed(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("hello")
    dock.append_message("final answer")
    dock.set_status("turn:analyzing", "Analyzing")

    tui._flush_committed(force=True)
    assert "final answer" in fake_stdout.text
    fake_stdout.text = ""

    dock.finish_status("turn:analyzing")
    tui._flush_committed()

    assert "final answer" not in fake_stdout.text
    assert tui._committed_line_count == len(dock.tree.render(tui._frame_width()))


def test_flushed_root_message_is_not_replayed_when_later_tools_are_added(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = False
    dock.begin_capture()
    dock.start_turn("再看看工作区新增的改动")
    tool = dock.start_tool(
        "Giting",
        "status",
        tool_name="git",
        raw_args={"args": "git status"},
    )
    dock.finish_tool_node(tool, "Giting", 0.1, True)
    tui._flush_committed()

    dock.append_message("相比上次 review，新增了几个文件的改动。让我看完所有变更。")
    tui._flush_committed()

    tool = dock.start_tool(
        "Giting",
        "diff",
        tool_name="git",
        raw_args={"args": "git diff"},
    )
    dock.finish_tool_node(tool, "Giting", 0.1, True)
    tui._flush_committed()

    assert fake_stdout.text.count("相比上次 review") == 1
    assert "让我看完所有变更。\n   ● Giting(\"diff\")" in fake_stdout.text
    assert "让我看完所有变更。\n\n" not in fake_stdout.text


def test_flush_committed_reconciles_in_place_tool_growth_by_node_identity(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = False
    dock.begin_capture()
    dock.start_turn("update the file")
    tool = dock.start_tool(
        "Editing",
        'file_path="src/example.py"',
        tool_name="edit",
        raw_args={"file_path": "src/example.py"},
    )
    dock.finish_tool_node(tool, "Editing", 0.1, True)
    dock.append_message("first narration")
    dock.append_message("second narration")
    tui._flush_committed(force=True)

    assert fake_stdout.text.count("first narration") == 1
    assert fake_stdout.text.count("second narration") == 1

    dock.append_file_change(
        """--- a/src/example.py
+++ b/src/example.py
@@ -1 +1,3 @@
 existing
+added one
+added two
""",
        parent=tool,
        tool_call_id="edit-1",
    )
    tui._flush_committed(force=True)

    assert "Update" in fake_stdout.text
    assert "added one" in fake_stdout.text
    assert "added two" in fake_stdout.text
    assert fake_stdout.text.count("first narration") == 1
    assert fake_stdout.text.count("second narration") == 1


@pytest.mark.parametrize("tty", [False, True])
def test_scrollback_allows_file_diff_nodes_but_hides_tool_results(
    tmp_path, monkeypatch, tty
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = tty
    if tty:
        tui._console = Console(
            file=None,
            force_terminal=True,
            width=80,
            height=30,
            _environ={},
        )
    dock.begin_capture()
    dock.start_turn("update the file")
    tool = dock.start_tool(
        "Editing",
        'file_path="src/example.py"',
        tool_name="edit",
        raw_args={"file_path": "src/example.py"},
    )
    dock.finish_tool_node(tool, "Editing", 0.1, True)
    dock.append_tool_result(
        "ordinary tool result that must stay in the active area",
        parent=tool,
        tool_call_id=tool.tool_call_id,
    )
    _append_previewed_file_diff(dock, tool, "added one")

    tui._flush_committed(force=True)

    output = Text.from_ansi(fake_stdout.text).plain
    assert 'Update("src/example.py")' in output
    assert "added one" in output
    assert "Full diff" in output
    assert "ordinary tool result that must stay in the active area" not in output


def test_scrollback_reconciles_consecutive_file_diff_calls(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = False
    dock.begin_capture()
    dock.start_turn("update files")
    for index in range(2):
        tool = dock.start_tool(
            "Editing",
            f'file_path="src/example{index}.py"',
            tool_name="edit",
            tool_call_id=f"edit-{index}",
            raw_args={"file_path": f"src/example{index}.py"},
        )
        dock.finish_tool_node(tool, "Editing", 0.1, True)
        dock.append_tool_result(
            f"ordinary result {index}",
            parent=tool,
            tool_call_id=tool.tool_call_id,
        )
        _append_previewed_file_diff(
            dock,
            tool,
            f"change {index}",
            path=f"src/example{index}.py",
        )
        tui._flush_committed(force=True)

    output = Text.from_ansi(fake_stdout.text).plain
    assert 'Update("src/example0.py")' in output
    assert 'Update("src/example1.py")' in output
    assert "change 0" in output
    assert "change 1" in output
    assert "ordinary result 0" not in output
    assert "ordinary result 1" not in output



def test_scrollback_file_diff_is_not_replayed_after_resize(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=30, _environ={})
    dock.begin_capture()
    dock.start_turn("resize after edit")
    tool = dock.start_tool(
        "Editing",
        'file_path="src/resize.py"',
        tool_name="edit",
        tool_call_id="edit-resize",
        raw_args={"file_path": "src/resize.py"},
    )
    dock.finish_tool_node(tool, "Editing", 0.1, True)
    dock.append_tool_result(
        "ordinary resize result",
        parent=tool,
        tool_call_id=tool.tool_call_id,
    )
    _append_previewed_file_diff(
        dock,
        tool,
        "resize diff",
        path="src/resize.py",
    )
    tui._flush_committed(force=True)
    fake_stdout.text = ""

    tui._console = Console(file=None, force_terminal=True, width=40, height=30, _environ={})
    tui._render_frame()

    resized_output = Text.from_ansi(fake_stdout.text).plain
    assert "ordinary resize result" in resized_output
    assert "resize diff" not in resized_output




def test_active_frame_reconciles_in_place_tool_growth_by_node_identity(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = False
    dock.begin_capture()
    dock.start_turn("update the file")
    tool = dock.start_tool(
        "Editing",
        'file_path="src/example.py"',
        tool_name="edit",
        raw_args={"file_path": "src/example.py"},
    )
    dock.finish_tool_node(tool, "Editing", 0.1, True)
    dock.append_message("first narration")
    dock.append_message("second narration")
    tui._flush_committed(force=True)

    dock.append_file_change(
        """--- a/src/example.py
+++ b/src/example.py
@@ -1 +1,3 @@
 existing
+added one
+added two
""",
        parent=tool,
        tool_call_id="edit-1",
    )

    rendered = "\n".join(_render_lines(tui))

    assert "Update" in rendered
    assert "added one" in rendered
    assert "added two" in rendered
    assert "first narration" not in rendered
    assert "second narration" not in rendered


@pytest.mark.parametrize("node_count", [255, 256, 260])
def test_logical_transcript_keeps_all_retained_nodes_before_viewport_projection(
    tmp_path, node_count
):
    tui = _tui(tmp_path)
    tui._console = Console(force_terminal=True, width=80, height=24, _environ={})
    nodes = tuple(
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"SOURCE-{index:03d}",
            payload={"index": index},
        )
        for index in range(node_count)
    )
    logical_rows = []
    for height in (6, 24, 60):
        tui._render_impl(height=height, capture_plan=True)
        logical = tui._render_plan.logical_plan
        transcript = logical.source_regions[0]
        plain = Text.from_ansi(transcript.ansi).plain
        assert transcript.visual_rows >= node_count
        assert all(f"SOURCE-{index:03d}" in plain for index in range(node_count))
        logical_rows.append(transcript.rows)

        physical = tui._physical_viewport_for_frame(
            logical,
            width=tui._frame_width(),
            term_height=height,
            frame_start_row=1,
        )
        target = tui._physical_target_lines(physical)
        assert len(target) == physical.frame_rows <= height
        assert physical.bottom.region.start_row <= physical.cursor_row <= height
        assert "SOURCE-000" not in "\n".join(target)

    assert logical_rows[0] == logical_rows[1] == logical_rows[2]
    assert tuple(dock.tree.root.children) == nodes
    assert [node.payload for node in nodes] == [
        {"index": index} for index in range(node_count)
    ]
    assert tui._committed_line_count == 0


def test_logical_transcript_does_not_replay_committed_nodes_after_resize(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "stdout", _FakeStdout())
    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(force_terminal=True, width=80, height=24, _environ={})
    for index in range(260):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"COMMITTED-{index:03d} " + "wide content " * 12,
        )
    tui._flush_committed(force=True)
    committed_projection = tui._committed_projection
    assert committed_projection is not None
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="UNCOMMITTED-LATEST",
    )

    for width, height in ((80, 6), (40, 60), (80, 24)):
        tui._console = Console(force_terminal=True, width=width, height=height, _environ={})
        tui._render_impl(height=height, capture_plan=True)
        plain = Text.from_ansi(tui._render_plan.logical_plan.source_regions[0].ansi).plain
        assert all(f"COMMITTED-{index:03d}" not in plain for index in range(260))
        assert "UNCOMMITTED-LATEST" in plain
        assert tui._committed_projection is committed_projection



def test_todo_physical_viewport_matches_bounded_frame_rows(tmp_path):
    tui = _tui(tmp_path)
    tui._console = Console(
        file=None,
        force_terminal=True,
        width=80,
        height=7,
        _environ={},
    )
    dock.set_todo_state(
        "2/4 done · 1 active · 1 pending",
        [
            {"content": "finished one", "status": "done"},
            {"content": "finished two", "status": "done"},
            {"content": "current task", "status": "active"},
            {"content": "next task", "status": "pending"},
        ],
    )

    rendered = tui._render_impl(height=7, capture_plan=True)
    actual = tui._capture_renderable(rendered, tui._frame_width()).splitlines()
    logical = tui._render_plan.logical_plan
    physical = tui._physical_viewport_for_frame(
        logical,
        width=tui._frame_width(),
        term_height=7,
        frame_start_row=1,
    )
    target = tui._physical_target_lines(physical)

    assert actual == target



def test_active_thinking_stream_does_not_leave_separator_in_transcript(
    tmp_path, monkeypatch
):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    dock.begin_capture()
    dock.start_turn("question")
    dock.set_stream("checking", phase="thinking")

    tui._render_impl(height=12, capture_plan=True)

    logical = tui._render_plan.logical_plan
    assert logical is not None
    transcript = logical.source_regions[0]
    assert transcript.visual_rows == 1
    assert all(row.strip() for row in transcript.rows)


def test_committed_tool_spacers_do_not_linger_in_active_frame(tmp_path, monkeypatch):
    """Committed result spacers must not accumulate even while the turn is live."""
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(
        file=fake_stdout, force_terminal=True, width=80, height=30, _environ={}
    )
    width = tui._frame_width()

    def active_lines():
        tree_lines, line_map = dock.tree.render_with_line_map(width)
        indexes = tui._active_identity_line_indexes(tree_lines, line_map)
        return [tree_lines[i] for i in indexes]

    dock.begin_capture()
    dock.start_turn("edit files")

    for i in range(20):
        tool = dock.start_tool(
            "Editing",
            f'file_path="f{i}.py"',
            tool_name="edit",
            raw_args={"file_path": f"f{i}.py"},
        )
        dock.finish_tool_node(tool, "Edit", 0.1, True)
        _append_previewed_file_diff(dock, tool, f"marker{i}", path=f"src/f{i}.py")
        tui._render_frame()
        tui._flush_committed(force=True)

        assert all(line.strip() for line in active_lines()), f"spacer after edit {i}"

    tool = dock.start_tool("Reading", tool_name="read")
    dock.finish_tool_node(tool, "Read", 0.1, True)
    dock.append_tool_result("result still needed midturn", parent=tool)
    tui._flush_committed(force=True)
    assert "result still needed midturn" in "\n".join(active_lines())

    dock.set_stream("final answer body")
    dock.commit_stream(refresh=False)
    dock.append_message("[dim]✻ 12s[/dim] · 3 calls", markup=True)
    dock.end_turn(outcome="completed")
    tui._flush_committed(force=True)

    assert all(line.strip() for line in active_lines())


def test_thinking_stream_renders_below_vibe_and_above_todo(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx_cli.render_activity.time.monotonic", lambda: 105.0)
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._busy = True
    tui._busy_started_at = 100.0

    dock.begin_capture()
    dock.start_turn("inspecting question")
    dock.set_stream("analysis in progress", phase="thinking")
    dock.set_todo_state(
        "0/2 done · 1 active · 1 pending",
        [
            {"content": "inspect behavior", "status": "active"},
            {"content": "write regression", "status": "pending"},
        ],
    )

    rendered = "\n".join(_rich_plain(line) for line in _render_lines(tui, width=80))

    assert "Thinking" in rendered
    assert "analysis in progress" in rendered
    assert "Todo:" in rendered

    vibe_pos = rendered.index("Thinking")
    thinking_pos = rendered.index("analysis in progress")
    todo_pos = rendered.index("Todo:")

    assert vibe_pos < thinking_pos < todo_pos


def test_bottom_dock_and_todo_locked_at_terminal_bottom_during_middle_scroll(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 20)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=20, _environ={})

    dock.begin_capture()
    dock.start_turn("test scroll isolation")
    for i in range(13):
        dock.append_message(f"committed line {i}")
    tui._committed_line_count = 13
    tui._visible_committed_rows = 13

    dock.set_todo_state(
        "1 task",
        [{"content": "pinned todo item", "status": "active"}],
    )

    tui._render_frame()
    initial_bottom_start = tui._last_bottom_start_row
    initial_bottom_rows = tui._last_bottom_rows
    assert initial_bottom_start + initial_bottom_rows - 1 == 20

    fake_stdout.text = ""

    for i in range(5):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"active line {i}",
            collapsed=False,
        )

    tui._render_frame()

    assert tui._last_bottom_start_row == initial_bottom_start
    assert tui._last_bottom_rows == initial_bottom_rows
    assert "\x1b[20;1H\n" not in fake_stdout.text
    scroll_bottom = 20 - initial_bottom_rows
    assert f"\x1b[1;{scroll_bottom}r" in fake_stdout.text
    writes = re.findall(r"\x1b\[(\d+);1H([^\x1b]*)", fake_stdout.text)
    assert not [(row, text) for row, text in writes if int(row) >= initial_bottom_start and text]
    assert "\x1b[J" not in fake_stdout.text


def test_flush_committed_overflow_without_anchor_defers_and_retains_transcript(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 10)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(
        file=fake_stdout, force_terminal=True, width=80, height=10, _environ={}
    )
    tui._has_rendered_frame = True
    tui._last_frame_start_row = 8
    tui._visible_committed_rows = 7

    dock.begin_capture()
    dock.start_turn("overflow commit")
    for i in range(5):
        dock.append_message(f"overflow line {i}")

    tui._flush_committed(force=True)

    assert tui._last_frame_start_row == 8
    assert tui._last_bottom_rows == 0
    assert fake_stdout.text == ""
    assert tui._committed_line_count == 0
    assert dock.consume_force_flush_request() is True
    assert "overflow line 4" in "\n".join(dock.tree.render(80))


def test_frame_scroll_plan_tiny_terminal_with_anchored_bottom_does_not_scroll_feed(tmp_path):
    tui = _tui(tmp_path)
    # term_height=5, fixed_bottom_rows=4 -> scroll_bottom = 1 < 2
    # Should not emit terminal-height hardware scroll feed!
    visible_after, scroll_ansi = tui._frame_scroll_plan(
        frame_rows=5,
        term_height=5,
        visible_rows=3,
        fixed_bottom_rows=4,
    )
    assert scroll_ansi == ""
    assert visible_after == 0



def test_commit_cache_invalidation_preserves_anchored_bottom_geometry(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(
        file=fake_stdout, force_terminal=True, width=80, height=12, _environ={}
    )
    tui._has_rendered_frame = True
    tui._last_bottom_start_row = 8
    tui._last_bottom_rows = 5

    tui._invalidate_frame_cache()

    assert tui._last_bottom_start_row == 8
    assert tui._last_bottom_rows == 5
    assert tui._bottom_dock_is_anchored(12) is True


@pytest.mark.parametrize("reason", ["clear", "resize", "terminal_submission_failure", "overflow"])
def test_physical_boundary_invalidates_bottom_anchor(tmp_path, reason):
    tui = _tui(tmp_path)
    tui._has_rendered_frame = True
    tui._last_bottom_start_row = 9
    tui._last_bottom_rows = 4
    tui._invalidate_layout(reason)
    assert not tui._bottom_dock_is_anchored(12)


@pytest.mark.parametrize("start, count", [(10, 1), (11, 1), (8, 15), (0, 1)])
def test_commit_payload_uses_lf_only_for_controlled_scrolling(tmp_path, monkeypatch, start, count):
    output = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 10)))
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=output, force_terminal=True, width=80, height=10, _environ={})
    tui._has_rendered_frame = start > 0
    tui._last_frame_start_row = start
    tui._visible_committed_rows = max(0, start - 1)
    tui._last_bottom_start_row = 8 if start else 0
    tui._last_bottom_rows = 3 if start else 0
    dock.begin_capture()
    for batch in range(2):
        dock.start_turn(f"batch-{batch}")
        for row in range(count):
            dock.append_message(f"payload-{batch}-{row}")
        output.text = ""
        tui._flush_committed(force=True)
        scroll = "\x1b[1;7r\x1b[7;1H\r\n\x1b[r"
        remaining = output.text.replace(scroll, "")
        assert "\n" not in remaining and "\r" not in remaining
        assert not re.search(r"\x1b\[[0-9;]*S", output.text)
        for index in range(count):
            assert remaining.count(f"payload-{batch}-{index}\x1b[K") == 1
        if start:
            assert "\x1b[1;7r" in output.text
            assert "\x1b[r" in output.text
        assert not re.search(r"\x1b\[(?:11|12);", output.text)


def test_commit_without_safe_region_is_deferred_without_settling(tmp_path, monkeypatch):
    output = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 3)))
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=output, force_terminal=True, width=80, height=3, _environ={})
    tui._has_rendered_frame = True
    tui._last_frame_start_row = 4
    tui._last_bottom_start_row = 1
    tui._last_bottom_rows = 3
    dock.begin_capture()
    dock.start_turn("retained transcript")
    dock.append_message("must not disappear")
    before = tui._committed_line_count
    tui._flush_committed(force=True)
    assert output.text == ""
    assert tui._committed_line_count == before
    assert dock.consume_force_flush_request() is True


@pytest.mark.parametrize("worker_mode", [False, True])
@pytest.mark.parametrize("visible_rows", [0, 3, 16])
def test_anchored_frame_uses_old_scroll_boundary_and_contiguous_start(
    tmp_path, monkeypatch, worker_mode, visible_rows
):
    output = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(
        shutil, "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 20)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=output, force_terminal=True, width=80, height=20, _environ={})
    tui._render_frame()
    old_bottom_rows = tui._last_bottom_rows
    tui._last_bottom_start_row = 20 - old_bottom_rows + 1
    tui._last_frame_start_row = 4
    tui._last_frame_rows = 17
    tui._visible_committed_rows = visible_rows
    assert tui._bottom_dock_is_anchored(20)

    class Worker:
        worker_mode = True

        def __init__(self):
            self.frames = []

        def submit_frame(self, batch):
            self.frames.append(batch)

    writer = Worker()
    if worker_mode:
        tui._terminal_writer = writer
    calls = []
    original = tui._frame_scroll_plan

    def record_scroll(frame_rows, term_height, **kwargs):
        result = original(frame_rows, term_height, **kwargs)
        calls.append((kwargs, result))
        return result

    monkeypatch.setattr(tui, "_frame_scroll_plan", record_scroll)
    output.text = ""
    tui._render_frame()

    assert len(calls) == 1
    kwargs, (visible_after, scroll_ansi) = calls[0]
    assert kwargs["visible_rows"] == visible_rows
    assert kwargs["fixed_bottom_rows"] == old_bottom_rows
    assert kwargs["scroll_bottom"] == 20 - old_bottom_rows
    assert tui._last_frame_start_row == visible_after + 1
    if worker_mode:
        batch = writer.frames[-1]
        assert batch.start_row == visible_after + 1
        assert batch.scroll_bottom == 20 - old_bottom_rows
        assert batch.scroll_ansi == scroll_ansi
    else:
        assert tui._visible_committed_rows == visible_after
        if not scroll_ansi:
            assert not tui._bottom_dock_is_anchored(20)


def test_frame_shrink_lifts_bottom_and_clears_old_tail(tmp_path, monkeypatch):
    output = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 20)))
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=output, force_terminal=True, width=80, height=20, _environ={})
    dock.begin_capture()
    dock.start_turn("question")
    nodes = [dock.tree.new_node(
        parent=dock.tree.root, node_type="message",
        header=f"active {i}", collapsed=False,
    ) for i in range(30)]
    tui._render_frame()
    assert tui._bottom_dock_is_anchored(20)
    start_row = tui._last_frame_start_row
    visible_rows = tui._visible_committed_rows

    for node in nodes:
        dock.tree.remove_node(node)
    output.text = ""
    tui._render_frame()

    assert tui._last_frame_start_row == start_row == visible_rows + 1
    assert tui._visible_committed_rows == visible_rows
    assert not tui._bottom_dock_is_anchored(20)
    assert "\x1b[20;1H\x1b[K" in output.text
    assert "\x1b[1;17r" not in output.text


def test_thinking_stream_records_correct_thinking_rows_in_frame_state(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._busy = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    dock.begin_capture()
    dock.start_turn("question")
    dock.set_stream("thinking line 1\nthinking line 2", phase="thinking")

    tui._render_frame()

    assert tui._last_busy_activity_thinking_rows == 2


@pytest.mark.asyncio
async def test_tool_finish_commit_and_recovery_frame_are_atomic(tmp_path, monkeypatch):
    """When a tool finishes and its row is committed, the recovery frame must
    be submitted immediately after the commit so the worker applies both in a
    single synchronized block; the vibe line must never be visibly blank."""
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(force_terminal=True, width=80, height=12, _environ={})
    tui._running = True
    tui._terminal_writer_required = True

    from voidx_cli.terminal_writer import TerminalWriter

    class SpyWriter(TerminalWriter):
        _started = True

        def __init__(self):
            super().__init__(stream=None)
            self.frames: list = []
            self.commits: list = []

        def submit_frame(self, batch):
            self.frames.append(batch)

        def submit_commit(self, **kwargs):
            self.commits.append(kwargs)
            return object()

        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            pass

    writer = SpyWriter()
    tui._terminal_writer = writer

    # Set up a committed state with a rendered frame.
    dock.begin_capture()
    dock.start_turn("task")
    tool = dock.start_tool("Editing", 'file_path="x.py"', tool_name="edit", tool_call_id="t1")
    dock.finish_tool_node(tool, "Editing", 0.1, True)
    tui._has_rendered_frame = True
    tui._last_frame_start_row = 6
    tui._last_frame_rows = 4
    tui._last_bottom_rows = 3
    tui._last_bottom_start_row = 9
    tui._committed_line_count = 0
    tui._committed_tree_revision = dock.tree.revision  # mark tree as committed
    tui._startup_committed = True  # skip startup path

    # Pre-render once so the frame geometry caches exist for the commit plan.
    tui._render_impl(height=12, capture_plan=True)
    render_plan = tui._render_plan
    assert render_plan is not None and render_plan.logical_plan is not None
    tui._last_render_plan = render_plan

    # Now finish the tool and flush; the commit should be followed by a recovery frame.
    tui._busy = True  # turn still running -> vibe row must be in the frame
    tui._busy_activity_verb = "Cogitating"
    dock.finish_tool_node(tool, "Editing", 0.5, True)
    tui._flush_committed()

    # The commit must carry the recovery frame bundled in the same batch,
    # so the worker can apply both in one synchronized block.
    assert len(writer.commits) == 1, f"expected 1 commit, got {len(writer.commits)}"
    assert len(writer.frames) == 0, f"expected no separate frame, got {len(writer.frames)}"
    frame = writer.commits[0].get("recovery_frame")
    assert frame is not None, "commit must carry a bundled recovery frame"
    assert frame.generation == tui._terminal_frame_generation
    plain = chr(10).join(frame.target_lines)
    assert "Cogitating" in plain


@pytest.mark.asyncio
async def test_commit_falls_back_to_deferred_frame_when_atomic_plan_unavailable(
    tmp_path, monkeypatch
):
    """When _render_frame_for_commit returns None (e.g. no logical_plan), the
    commit must still proceed and the recovery frame must be deferred to the
    normal _render_frame path after the commit settles."""
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(force_terminal=True, width=80, height=12, _environ={})
    tui._running = True
    tui._terminal_writer_required = True

    from voidx_cli.terminal_writer import TerminalWriter

    class SpyWriter(TerminalWriter):
        _started = True

        def __init__(self):
            super().__init__(stream=None)
            self.frames: list = []
            self.commits: list = []

        def submit_frame(self, batch):
            self.frames.append(batch)

        def submit_commit(self, **kwargs):
            self.commits.append(kwargs)
            return object()

        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            pass

    writer = SpyWriter()
    tui._terminal_writer = writer

    dock.begin_capture()
    dock.start_turn("task")
    tool = dock.start_tool("Editing", 'file_path="x.py"', tool_name="edit", tool_call_id="t1")
    dock.finish_tool_node(tool, "Editing", 0.1, True)
    tui._has_rendered_frame = True
    tui._last_frame_start_row = 6
    tui._last_frame_rows = 4
    tui._last_bottom_rows = 3
    tui._last_bottom_start_row = 9
    tui._committed_line_count = 0
    tui._committed_tree_revision = dock.tree.revision
    tui._startup_committed = True
    # Atomic plan unavailable -> the commit must fall back to deferred recovery.
    monkeypatch.setattr(tui, "_render_frame_for_commit", lambda *a, **k: None)

    dock.finish_tool_node(tool, "Editing", 0.5, True)
    tui._flush_committed()

    # Commit must proceed, but no recovery frame is bundled or submitted.
    assert len(writer.commits) == 1
    assert writer.commits[0].get("recovery_frame") is None
    assert len(writer.frames) == 0
