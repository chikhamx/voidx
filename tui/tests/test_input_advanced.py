from tui_helpers import *  # noqa: F403

import asyncio
import contextlib
import os
import sys
from types import SimpleNamespace

import pytest
from rich.console import Console

from voidx.config import Settings
from voidx.presentation.commands import COMMANDS
from voidx_cli import PureTui


def _write_skill(workspace, name: str, description: str) -> None:
    skill_dir = workspace / ".voidx" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nSkill body",
        encoding="utf-8",
    )


def test_empty_enter_on_empty_input_is_noop(tmp_path):
    tui = _tui(tmp_path)

    changed = tui._process_input(b"\r")

    assert changed is False
    assert tui._get_input_text() == ""
    assert tui._queue.empty()


@pytest.mark.parametrize(
    "sequence,expected_input",
    [
        (b"\x1b[Mabc", "abc"),
    ],
)
def test_legacy_mouse_sequences_become_plain_text(tmp_path, sequence, expected_input):
    """Without mouse tracking enabled, mouse escape sequences degrade to
    printable characters — ESC is consumed, M abc are inserted as text."""
    tui = _tui(tmp_path)

    changed = tui._process_input(sequence)

    assert changed is True
    assert tui._get_input_text() == expected_input
    assert tui._queue.empty()


@pytest.mark.parametrize(
    "sequence",
    [
        b"\x1b[<64;10;5M",
        b"\x1b[<65;10;5m",
        b"\x1b[64;10;5M",
    ],
)
def test_mouse_scroll_is_noop_without_tracking(tmp_path, sequence):
    """Without mouse tracking, SGR mouse sequences are consumed as CSI
    with no side effects — the terminal won't send them anyway."""
    tui = _tui(tmp_path)

    changed = tui._process_input(sequence)

    assert changed is False
    assert tui._get_input_text() == ""
    assert tui._queue.empty()


def test_shift_enter_csi_u_inserts_newline_without_submit(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    tui._process_input(b"\x1b[13;2u")

    assert tui._get_input_text() == "hello\n"
    assert tui._queue.empty()


def test_shift_enter_modify_other_keys_inserts_newline_without_submit(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    tui._process_input(b"\x1b[27;2;13~")

    assert tui._get_input_text() == "hello\n"
    assert tui._queue.empty()


def test_ctrl_j_in_tty_mode_inserts_newline_without_submit(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    tui._process_input(b"\n")

    assert tui._get_input_text() == "hello\n"
    assert tui._queue.empty()


def test_ctrl_a_moves_to_current_line_start(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["first", "second"]
    tui._cursor_row = 1
    tui._cursor_col = 4

    changed = tui._process_input(b"\x01")

    assert changed is True
    assert tui._cursor_row == 1
    assert tui._cursor_col == 0


def test_ctrl_e_moves_to_current_line_end(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["first", "second"]
    tui._cursor_row = 0
    tui._cursor_col = 2

    changed = tui._process_input(b"\x05")

    assert changed is True
    assert tui._cursor_row == 0
    assert tui._cursor_col == len("first")


def test_ctrl_a_e_ignore_active_choice(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["draft"]
    tui._cursor_col = 3
    tui._active_choice = [("y", "y", ""), ("n", "n", "")]

    tui._process_input(b"\x01\x05")

    assert tui._cursor_col == 3
    assert tui._choice_queue.empty()
    assert tui._queue.empty()


@pytest.mark.parametrize(
    "sequence,expected_col",
    [
        (b"\x1b[H", 0),
        (b"\x1b[F", len("draft")),
        (b"\x1b[1~", 0),
        (b"\x1b[4~", len("draft")),
        (b"\x1b[7~", 0),
        (b"\x1b[8~", len("draft")),
        (b"\x1bOH", 0),
        (b"\x1bOF", len("draft")),
    ],
)
def test_home_end_escape_sequences_move_cursor(tmp_path, sequence, expected_col):
    tui = _tui(tmp_path)
    tui._input_lines = ["draft"]
    tui._cursor_col = 2

    changed = tui._process_input(sequence)

    assert changed is True
    assert tui._cursor_col == expected_col
    assert tui._get_input_text() == "draft"


@pytest.mark.parametrize(
    "sequence,expected_col",
    [
        (b"\x1b[1~", 0),
        (b"\x1b[4~", len("second")),
    ],
)
def test_home_end_escape_sequences_keep_multiline_row(tmp_path, sequence, expected_col):
    tui = _tui(tmp_path)
    tui._input_lines = ["first", "second", "third"]
    tui._cursor_row = 1
    tui._cursor_col = 3

    tui._process_input(sequence)

    assert tui._cursor_row == 1
    assert tui._cursor_col == expected_col
    assert tui._get_input_text() == "first\nsecond\nthird"


def test_multiline_input_render_uses_indentation_without_visible_newline_symbol(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["1、", "2、", "3、"]
    tui._cursor_row = 2
    tui._cursor_col = 2

    console = Console(file=None, force_terminal=False, width=80, height=24, _environ={})
    with console.capture() as capture:
        console.print(tui._render_impl())
    rendered = capture.get()

    assert "↵" not in rendered
    assert "❯ 1、" in rendered
    assert "  2、" in rendered


def test_csi_sequence_consumes_full_sequence_after_text(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["draft"]
    tui._cursor_col = len("draft")
    tui._record_history("old")
    tui._process_input(b"a\x1b[A")

    assert tui._get_input_text() == "old"


def test_multiline_arrow_up_down_moves_within_input_before_history(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["first", "second", "third"]
    tui._cursor_row = 2
    tui._cursor_col = len("third")
    tui._record_history("old")

    tui._process_input(b"\x1b[A")
    assert tui._cursor_row == 1
    assert tui._get_input_text() == "first\nsecond\nthird"

    tui._process_input(b"\x1b[A")
    assert tui._cursor_row == 0
    assert tui._get_input_text() == "first\nsecond\nthird"

    tui._process_input(b"\x1b[A")
    assert tui._get_input_text() == "old"

    tui._process_input(b"\x1b[B")
    assert tui._get_input_text() == "first\nsecond\nthird"


def test_history_navigation_does_not_stall_on_slash_command_entry(tmp_path):
    tui = _tui(tmp_path)
    tui._record_history("hello")
    tui._record_history("/help")
    tui._input_lines = ["draft"]
    tui._cursor_col = len("draft")

    # Up → loads "/help" (most recent)
    tui._process_input(b"\x1b[A")
    assert tui._get_input_text() == "/help"

    # Up again → should continue to "hello", not stall in command panel
    tui._process_input(b"\x1b[A")
    assert tui._get_input_text() == "hello"

    # Down → back to "/help"
    tui._process_input(b"\x1b[B")
    assert tui._get_input_text() == "/help"

    # Down → back to draft
    tui._process_input(b"\x1b[B")
    assert tui._get_input_text() == "draft"


def test_multiline_arrow_navigation_clamps_column(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["short", "a much longer line"]
    tui._cursor_row = 1
    tui._cursor_col = len("a much longer line")

    tui._process_input(b"\x1b[A")

    assert tui._cursor_row == 0
    assert tui._cursor_col == len("short")


def test_ctrl_c_requires_second_empty_press(tmp_path):
    tui = _tui(tmp_path)

    tui._handle_interrupt()

    assert tui._queue.empty()
    assert tui._notice == "Press Ctrl-C again to exit"
    assert tui._ctrl_c_armed is True

    tui._handle_interrupt()

    assert tui._queue.get_nowait() is None


def test_ctrl_c_deadline_requires_fresh_second_press(tmp_path, monkeypatch):
    now = 100.0
    monkeypatch.setattr("voidx_cli.app.time.monotonic", lambda: now)
    tui = _tui(tmp_path)

    tui._handle_interrupt()
    assert tui._ctrl_c_armed is True

    now = 104.0
    tui._handle_interrupt()

    assert tui._queue.empty()
    assert tui._notice == "Press Ctrl-C again to exit"
    assert tui._ctrl_c_deadline == 107.0


def test_ctrl_c_clears_input_before_arming_exit(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    tui._handle_interrupt()

    assert tui._get_input_text() == ""
    assert tui._queue.empty()
    assert "Input cleared" in tui._notice

    tui._handle_interrupt()

    assert tui._queue.empty()
    assert tui._notice == "Press Ctrl-C again to exit"


def test_typing_after_ctrl_c_resets_exit_prompt(tmp_path):
    tui = _tui(tmp_path)

    tui._handle_interrupt()
    tui._insert_text("h")

    assert tui._ctrl_c_armed is False
    assert tui._notice == ""
    assert tui._queue.empty()


@pytest.mark.asyncio
async def test_ctrl_c_cancels_active_submit_task(tmp_path):
    tui = _tui(tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def on_submit(_text: str) -> bool:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    consumer = asyncio.create_task(tui._consume(on_submit))
    tui._queue.put_nowait("slow")
    await started.wait()

    tui._handle_interrupt()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)

    assert tui._get_input_text() == "slow"
    assert tui._notice == "Interrupted. Restored last message for editing."
    assert tui._current_submit_task is None
    assert tui._busy is False

    tui._queue.put_nowait(None)
    await asyncio.wait_for(consumer, timeout=1)


@pytest.mark.asyncio
async def test_read_input_raw_uses_add_reader_for_pipe_bytes(tmp_path):
    if sys.platform == "win32":
        pytest.skip("add_reader coverage is POSIX-only")
    read_fd, write_fd = os.pipe()
    tui = _tui(tmp_path)
    tui._stdin_fd = read_fd
    try:
        os.write(write_fd, b"abc")
        data = await asyncio.wait_for(tui._read_input_raw(), timeout=1)
    finally:
        tui._close_stdin_reader()
        os.close(write_fd)
        os.close(read_fd)

    assert data == b"abc"


@pytest.mark.asyncio
async def test_read_input_raw_does_not_mark_terminal_fd_nonblocking(tmp_path):
    if sys.platform == "win32":
        pytest.skip("pty coverage is POSIX-only")
    import fcntl
    import pty
    import termios

    master_fd, slave_fd = pty.openpty()
    stdout_like_fd = os.dup(slave_fd)
    old_attrs = termios.tcgetattr(slave_fd)
    new_attrs = termios.tcgetattr(slave_fd)
    new_attrs[3] &= ~(termios.ICANON | termios.ECHO)
    new_attrs[6][termios.VMIN] = 1
    new_attrs[6][termios.VTIME] = 0
    termios.tcsetattr(slave_fd, termios.TCSANOW, new_attrs)

    def is_nonblocking(fd: int) -> bool:
        return bool(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK)

    tui = _tui(tmp_path)
    tui._stdin_fd = slave_fd
    task = asyncio.create_task(tui._read_input_raw())
    try:
        await asyncio.sleep(0)
        assert not is_nonblocking(slave_fd)
        assert not is_nonblocking(stdout_like_fd)

        os.write(master_fd, b"x")
        data = await asyncio.wait_for(task, timeout=1)

        assert data == b"x"
        assert not is_nonblocking(slave_fd)
        assert not is_nonblocking(stdout_like_fd)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        termios.tcsetattr(slave_fd, termios.TCSANOW, old_attrs)
        os.close(stdout_like_fd)
        os.close(slave_fd)
        os.close(master_fd)


@pytest.mark.asyncio
async def test_read_input_raw_returns_ctrl_d_on_stdin_eof(tmp_path):
    if sys.platform == "win32":
        pytest.skip("add_reader coverage is POSIX-only")
    read_fd, write_fd = os.pipe()
    tui = _tui(tmp_path)
    tui._stdin_fd = read_fd
    os.close(write_fd)
    try:
        data = await asyncio.wait_for(tui._read_input_raw(), timeout=1)
    finally:
        tui._close_stdin_reader()
        os.close(read_fd)

    assert data == b"\x04"


@pytest.mark.asyncio
async def test_run_finally_does_not_mask_missing_workspace(monkeypatch):
    class FakeStdout:
        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            pass

    async def fake_read_input_raw():
        return b"\x04"

    tui = PureTui(SimpleNamespace(), COMMANDS)
    tui._stdin_fd = 99
    monkeypatch.setattr(os, "isatty", lambda _fd: True)
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    monkeypatch.setattr(tui, "_setup_terminal", lambda: None)
    monkeypatch.setattr(tui, "_restore_terminal", lambda: None)
    monkeypatch.setattr(tui, "_render_frame", lambda: None)
    monkeypatch.setattr(tui, "_move_to_frame_end_sequence", lambda: "")
    monkeypatch.setattr(tui, "_read_input_raw", fake_read_input_raw)

    async def on_submit(_text: str) -> bool:
        return True

    await tui.run(on_submit)
