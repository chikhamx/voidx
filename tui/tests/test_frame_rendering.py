from tui_helpers import *  # noqa: F403

import os
import re
import shutil
import sys

from rich.console import Console

from voidx.presentation.output.dock import dock

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
    assert fake_stdout.text.startswith("\x1b[4;1H\x1b[J")


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
        assert fake_stdout.text[:clear_pos].startswith("\x1b[12;1H\n\x1b[3;1H")


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


def test_flush_committed_counts_blank_separator_flushed_by_itself(tmp_path, monkeypatch):
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

    dock.set_stream("second assistant")
    tui._flush_committed()

    assert tui._committed_line_count == 5
    assert tui._visible_committed_rows == 5


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


def test_final_flush_clears_existing_frame_before_printing_answer(tmp_path, monkeypatch):
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

    clear_index = fake_stdout.text.find("\x1b[J")
    answer_index = fake_stdout.text.find("改好了")
    assert clear_index != -1
    assert answer_index != -1
    assert clear_index < answer_index


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
    assert "让我看完所有变更。\n\n   ● Giting(\"git diff\")" in fake_stdout.text
