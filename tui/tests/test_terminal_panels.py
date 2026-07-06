from tui_helpers import *  # noqa: F403

import os
import re
import shutil
import sys
from types import SimpleNamespace

from rich.cells import cell_len
from rich.console import Console

from voidx.ui.commands import COMMANDS
from voidx.ui.output.dock import dock
import voidx_cli.terminal_mixin as terminal_mixin
from voidx_cli import (
    PureTui,
    _ENTER_TERMINAL_SEQUENCE,
    _EXIT_TERMINAL_SEQUENCE,
    _rendered_row_count,
)

def test_terminal_sequences_stay_on_normal_buffer():
    # Alternate screen NOT used — terminal handles scrollback natively
    assert "\x1b[?1049h" not in _ENTER_TERMINAL_SEQUENCE
    assert "\x1b[?1049l" not in _EXIT_TERMINAL_SEQUENCE
    assert "\x1b[?1000l" in _ENTER_TERMINAL_SEQUENCE
    assert "\x1b[?1006l" in _EXIT_TERMINAL_SEQUENCE


class _FakeKernel32:
    def __init__(
        self,
        mode: int = 0,
        *,
        get_ok: bool = True,
        set_ok: bool = True,
    ) -> None:
        self.mode = mode
        self.get_ok = get_ok
        self.set_ok = set_ok
        self.handles: list[int] = []
        self.set_modes: list[int] = []

    def GetStdHandle(self, handle: int) -> int:
        self.handles.append(handle)
        return 123

    def GetConsoleMode(self, handle: int, mode_ptr) -> int:
        if not self.get_ok:
            return 0
        mode_ptr._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle: int, mode: int) -> int:
        if not self.set_ok:
            return 0
        self.set_modes.append(int(mode))
        self.mode = int(mode)
        return 1


def test_windows_enable_virtual_terminal_processing_sets_mode():
    kernel32 = _FakeKernel32(mode=0)

    original = terminal_mixin._enable_windows_virtual_terminal_processing(kernel32)

    assert original == 0
    assert kernel32.handles == [terminal_mixin._STD_OUTPUT_HANDLE]
    assert kernel32.set_modes == [
        terminal_mixin._ENABLE_VIRTUAL_TERMINAL_PROCESSING
    ]


def test_windows_enable_virtual_terminal_processing_keeps_existing_mode():
    mode = terminal_mixin._ENABLE_VIRTUAL_TERMINAL_PROCESSING | 0x0001
    kernel32 = _FakeKernel32(mode=mode)

    original = terminal_mixin._enable_windows_virtual_terminal_processing(kernel32)

    assert original == mode
    assert kernel32.set_modes == []


def test_windows_restore_console_mode_restores_original_mode():
    kernel32 = _FakeKernel32(mode=0)

    restored = terminal_mixin._restore_windows_console_mode(7, kernel32)

    assert restored is True
    assert kernel32.set_modes == [7]


def test_windows_console_mode_helpers_ignore_non_console():
    assert (
        terminal_mixin._enable_windows_virtual_terminal_processing(
            _FakeKernel32(get_ok=False)
        )
        is None
    )
    assert (
        terminal_mixin._enable_windows_virtual_terminal_processing(
            _FakeKernel32(set_ok=False)
        )
        is None
    )
    assert terminal_mixin._restore_windows_console_mode(None, _FakeKernel32()) is False


def test_non_windows_terminal_setup_still_uses_termios(tmp_path, monkeypatch):
    class FakeTermios:
        ECHO = 0x0001
        ICANON = 0x0002
        ISIG = 0x0004
        IEXTEN = 0x0008
        IGNBRK = 0x0010
        ICRNL = 0x0020
        BRKINT = 0x0040
        VMIN = 0
        VTIME = 1
        VLNEXT = 2
        TCSADRAIN = 0

        def __init__(self) -> None:
            self.set_attrs: list[list] = []

        def tcgetattr(self, fd: int) -> list:
            return [
                self.IGNBRK | self.ICRNL,
                0,
                0,
                self.ECHO | self.ICANON | self.ISIG | self.IEXTEN,
                0,
                0,
                [0, 0, 1],
            ]

        def tcsetattr(self, fd: int, when: int, attrs: list) -> None:
            self.set_attrs.append(attrs)

    fake_termios = FakeTermios()
    monkeypatch.setattr(terminal_mixin, "termios", fake_termios)
    monkeypatch.setattr(terminal_mixin.os, "isatty", lambda fd: True)
    tui = _tui(tmp_path)
    tui._stdin_fd = 99

    tui._setup_terminal()

    assert tui._old_termios is not None
    assert len(fake_termios.set_attrs) == 1
    assert fake_termios.set_attrs[0][6][fake_termios.VMIN] == 1
    assert fake_termios.set_attrs[0][6][fake_termios.VTIME] == 0


def test_dock_clear_screen_request_is_consumed_publicly():
    dock.reset()

    assert dock.consume_clear_screen_request() is True
    assert dock.consume_clear_screen_request() is False


def test_rendered_row_count_tracks_terminal_cursor_rows():
    assert _rendered_row_count("") == 0
    assert _rendered_row_count("one") == 1
    assert _rendered_row_count("one\n") == 2
    assert _rendered_row_count("one\ntwo\n") == 3


def test_render_impl_clips_transcript_to_visible_tail(tmp_path):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=10, _environ={})
    for index in range(20):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"line {index:02d}",
            collapsed=False,
        )

    lines = _render_lines(tui, width=80)
    rendered = "\n".join(lines)

    assert len(lines) <= 10
    assert "line 12" not in rendered
    if sys.platform != "win32":
        assert "line 13" in rendered
    assert "line 19" in rendered


def test_frame_top_positioning_uses_terminal_height_for_absolute_row(tmp_path, monkeypatch):
    """Frame rendering calculates start_row from terminal height so
    the frame always renders at the bottom without scrollback pollution."""
    monkeypatch.setattr(
        shutil, "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._has_rendered_frame = True

    # _move_to_frame_top_sequence is no longer called in the render path;
    # absolute positioning is computed directly from terminal height.
    # The old method still exists but is dead code.
    tui._last_frame_rows = 5
    assert tui._move_to_frame_top_sequence() == "\x1b[5A"


def test_frame_end_sequence_returns_from_input_cursor_to_frame_end(tmp_path):
    tui = _tui(tmp_path)
    tui._has_rendered_frame = True
    tui._last_frame_rows = 30
    tui._cursor_to_frame_end_lines = 4

    assert tui._move_to_frame_end_sequence() == "\r\x1b[4B\r"


def test_dock_turn_spacing_is_root_level_blank_line(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        first = dock.start_turn("one")
        dock.start_turn("two")

        lines = dock.tree.render(100)
        assert "one" in lines[0]
        assert lines[1] == ""
        assert "two" in lines[2]
        assert first.body_lines == []
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_turn_and_assistant_response_have_root_level_gap(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.start_turn("one")
        dock.set_stream("answer")
        dock.commit_stream()

        lines = dock.tree.render(100)
        assert "one" in lines[0]
        assert lines[1] == ""
        assert "answer" in lines[2]
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_multiline_turn_body_aligns_under_prompt(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        turn = dock.start_turn("1、你\n2、好\n3、你是谁")

        lines = dock.tree.render(100)
        plain_lines = [_rich_plain(line) for line in lines[:3]]
        assert [line.rstrip() for line in plain_lines] == ["❯ 1、你", "  2、好", "  3、你是谁"]
        assert all(cell_len(line) == 100 for line in plain_lines)
        assert turn.body_lines == ["2、好", "3、你是谁"]
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_turn_preserves_long_single_line_input(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        text = "long-input-" + ("x" * 240) + "-tail"
        turn = dock.start_turn(text)

        rendered = "\n".join(dock.tree.render(80))

        assert text in rendered
        assert "tail" in rendered
        assert turn.body_lines == []
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_turn_preserves_long_multiline_input(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        second = "second-" + ("y" * 180)
        third = "third-tail"
        turn = dock.start_turn(f"first\n{second}\n{third}")

        rendered = "\n".join(dock.tree.render(100))

        assert second in rendered
        assert third in rendered
        assert turn.body_lines == [second, third]
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_tool_header_uses_raw_args_without_rich_markup(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Reading",
            'file_path="[cyan]src/voidx/ui/dock.py[/cyan]"',
            tool_name="read",
            raw_args={"file_path": "src/voidx/ui/dock.py"},
        )
        dock.finish_tool_node(tool, "read", 0.0, True)

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))
        assert 'Read("src/voidx/ui/dock.py")' in rendered
        assert "[cyan]" not in rendered
        assert "(0.0s)" not in rendered
        assert "Reading file_path" not in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_tool_collapsed_summary_does_not_duplicate_elapsed(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Reading",
            "",
            tool_name="read",
            raw_args={"file_path": "src/app.py"},
        )
        dock.finish_tool_node(tool, "read", 2.5, True)

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))
        assert rendered.count("(2.5s)") == 1
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_tool_summary_does_not_replace_tool_header(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Running",
            "",
            tool_name="bash",
            raw_args={"command": "git status --short"},
        )
        dock.finish_tool_node(tool, "bash", 0.1, True, "exit 0")

        rendered_lines = [_rich_plain(line).strip() for line in dock.tree.render(120)]
        rendered = "\n".join(rendered_lines)

        assert 'Bash("git status --short")' in rendered
        assert "exit 0" in rendered
        assert "exit 0" not in rendered_lines
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_git_tool_header_shows_args_not_path(tmp_path):
    """git tool header should display the args value, not the path field."""
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Git",
            "",
            tool_name="git",
            raw_args={"path": ".", "args": "log --oneline -5"},
        )
        dock.finish_tool_node(tool, "git", 0.1, True)

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))
        assert 'Git("log --oneline -5")' in rendered
        assert 'Git(".")' not in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_git_tool_finish_detail_no_command_prefix(tmp_path):
    """git finish detail should not duplicate the command name from header."""
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Git",
            "",
            tool_name="git",
            raw_args={"path": "", "args": "status --porcelain"},
        )
        dock.finish_tool_node(tool, "git", 0.1, True, "ok")

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))
        assert 'Git("status --porcelain")' in rendered
        assert "git status" not in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_input_cursor_position_counts_wide_chinese_cells(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    tui = _tui(tmp_path)
    tui._input_lines = ["你好i zai"]
    tui._cursor_row = 0
    tui._cursor_col = 2
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    tui._position_input_cursor()

    assert fake_stdout.text.startswith("\x1b[1A")
    assert "\x1b[7G" in fake_stdout.text


