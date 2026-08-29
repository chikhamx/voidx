from tui_helpers import *  # noqa: F403

import asyncio
import contextlib
import os
import sys
import threading
from types import SimpleNamespace

import pytest
from rich.console import Console

from voidx.config import Settings
from voidx.presentation.commands import COMMANDS
from voidx.presentation.output.dock import dock
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


@pytest.mark.asyncio
async def test_run_restores_terminal_before_transcript_export(tmp_path, monkeypatch):
    import voidx_cli.app as app_module

    events: list[object] = []

    async def fake_read_input_raw():
        return b"\x04"

    tui = _tui(tmp_path)
    tui._stdin_fd = 99
    monkeypatch.setattr(os, "isatty", lambda _fd: True)
    monkeypatch.setattr(tui, "_setup_terminal", lambda: None)
    monkeypatch.setattr(tui, "_restore_terminal", lambda: events.append("restore"))
    monkeypatch.setattr(tui, "_render_frame", lambda: None)
    monkeypatch.setattr(tui, "_move_to_frame_end_sequence", lambda: "")
    monkeypatch.setattr(tui, "_read_input_raw", fake_read_input_raw)
    monkeypatch.setattr(
        tui,
        "_flush_committed",
        lambda *, force=False: events.append(("flush_committed", force)),
    )
    async def fake_drain_async():
        events.append("writer_drain")

    monkeypatch.setattr(tui._terminal_writer, "drain_async", fake_drain_async)
    monkeypatch.setattr(
        app_module,
        "_dump_transcript_log",
        lambda *args, **kwargs: events.append("dump"),
    )

    async def on_submit(_text: str) -> bool:
        return True

    await tui.run(on_submit)

    force_flush = events.index(("flush_committed", True))
    writer_drain = events.index("writer_drain")
    restore = events.index("restore")
    dump = events.index("dump")
    assert force_flush < writer_drain < restore < dump


@pytest.mark.asyncio
async def test_pending_posix_read_cancel_removes_registered_reader(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("add_reader cancellation coverage is POSIX-only")
    read_fd, write_fd = os.pipe()
    tui = _tui(tmp_path)
    tui._stdin_fd = read_fd
    loop = asyncio.get_running_loop()
    original_add_reader = loop.add_reader
    original_remove_reader = loop.remove_reader
    added = []
    removed = []

    def tracked_add_reader(fd, callback, *args):
        added.append(fd)
        return original_add_reader(fd, callback, *args)

    def tracked_remove_reader(fd):
        removed.append(fd)
        return original_remove_reader(fd)

    monkeypatch.setattr(loop, "add_reader", tracked_add_reader)
    monkeypatch.setattr(loop, "remove_reader", tracked_remove_reader)
    task = asyncio.create_task(tui._read_input_raw())
    try:
        await asyncio.sleep(0)
        assert added == [read_fd]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert task.done()
        assert removed == [read_fd]
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        os.close(write_fd)
        os.close(read_fd)


class _LifecycleWriter:
    def __init__(
        self,
        events,
        *,
        start_error=None,
        wait_error_kind=None,
        wait_errors=None,
        drain_error=None,
        shutdown_error=None,
    ) -> None:
        self.events = events
        self.start_error = start_error
        self.wait_error_kind = wait_error_kind
        self.wait_errors = dict(wait_errors or {})
        self.drain_error = drain_error
        self.shutdown_error = shutdown_error
        self.started = False
        self.on_error = None
        self.on_frame_result = None
        self._token_id = 0

    @property
    def worker_mode(self) -> bool:
        return self.started

    def start(self, *, loop, on_frame_result, on_error) -> None:
        self.events.append("writer_start")
        if self.start_error is not None:
            raise self.start_error
        self.started = True
        self.on_frame_result = on_frame_result
        self.on_error = on_error

    def submit_barrier(self, *, kind, ansi="", invalidate_frame=False):
        self._token_id += 1
        token = (kind, self._token_id)
        self.events.append(("barrier", kind, ansi))
        return token

    async def wait(self, token) -> None:
        self.events.append(("wait", token[0]))
        error = self.wait_errors.get(token[0])
        if error is not None:
            raise error
        if token[0] == self.wait_error_kind:
            raise RuntimeError(f"{token[0]} wait failed")

    async def drain_async(self) -> None:
        self.events.append("drain")
        if self.drain_error is not None:
            raise self.drain_error

    async def shutdown_async(self) -> None:
        self.events.append("shutdown")
        self.started = False
        if self.shutdown_error is not None:
            raise self.shutdown_error

    def write(self, value: str) -> int:
        raise AssertionError(f"TTY lifecycle used synchronous write: {value!r}")

    def flush(self) -> None:
        raise AssertionError("TTY lifecycle used synchronous flush")


def _prepare_lifecycle_tui(tmp_path, monkeypatch, writer):
    import voidx_cli.app as app_module

    events = writer.events
    tui = _tui(tmp_path)
    tui._stdin_fd = 99
    tui._terminal_writer = writer
    monkeypatch.setattr(os, "isatty", lambda _fd: True)
    monkeypatch.setattr(tui, "_setup_terminal", lambda: events.append("setup"))
    monkeypatch.setattr(tui, "_restore_terminal", lambda: events.append("restore_terminal"))
    monkeypatch.setattr(tui, "_move_to_frame_end_sequence", lambda: "<move>")
    monkeypatch.setattr(tui, "_render_frame", lambda: events.append("render"))
    monkeypatch.setattr(
        app_module,
        "install_external_log_bridge",
        lambda _name: (
            events.append("external_install")
            or (lambda: events.append("external_restore"))
        ),
    )
    monkeypatch.setattr(
        app_module,
        "_dump_transcript_log",
        lambda *args, **kwargs: events.append("dump"),
    )
    return tui


@pytest.mark.asyncio
async def test_tty_run_waits_for_startup_barrier_before_producers(tmp_path, monkeypatch):
    events = []
    writer = _LifecycleWriter(events)
    tui = _prepare_lifecycle_tui(tmp_path, monkeypatch, writer)

    async def consume(_on_submit):
        events.append("consumer_start")
        await asyncio.Event().wait()

    async def read_input():
        events.append("input_start")
        return b"\x04"

    monkeypatch.setattr(tui, "_consume", consume)
    monkeypatch.setattr(tui, "_read_input_raw", read_input)
    monkeypatch.setattr(
        tui,
        "_flush_committed",
        lambda *, force=False: events.append(("flush_committed", force)),
    )

    async def on_submit(_text: str) -> bool:
        return True

    await tui.run(on_submit)

    startup_barrier = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple) and event[:2] == ("barrier", "startup")
    )
    startup_wait = events.index(("wait", "startup"))
    assert events.index("setup") < events.index("writer_start") < startup_barrier
    assert startup_barrier < startup_wait < events.index("consumer_start")
    assert startup_wait < events.index(("flush_committed", True))
    assert startup_wait < events.index("render") < events.index("input_start")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["setup", "start", "startup_wait"])
async def test_tty_startup_failures_restore_and_cleanup(
    tmp_path, monkeypatch, failure_phase
):
    events = []
    original = RuntimeError(f"{failure_phase} failed")
    writer = _LifecycleWriter(
        events,
        start_error=original if failure_phase == "start" else None,
        wait_error_kind="startup" if failure_phase == "startup_wait" else None,
    )
    tui = _prepare_lifecycle_tui(tmp_path, monkeypatch, writer)
    if failure_phase == "setup":
        def fail_setup():
            events.append("setup")
            raise original
        monkeypatch.setattr(tui, "_setup_terminal", fail_setup)

    async def on_submit(_text: str) -> bool:
        return True

    with pytest.raises(RuntimeError) as caught:
        await tui.run(on_submit)

    if failure_phase != "startup_wait":
        assert caught.value is original
    else:
        assert str(caught.value) == "startup wait failed"
    assert "restore_terminal" in events
    assert dock._refresh_callback is None
    assert dock._width_provider is None
    if failure_phase == "startup_wait":
        assert "shutdown" in events
    else:
        assert "shutdown" not in events


@pytest.mark.asyncio
async def test_writer_failure_wakes_and_cancels_pending_tty_input(tmp_path, monkeypatch):
    events = []
    writer = _LifecycleWriter(events)
    tui = _prepare_lifecycle_tui(tmp_path, monkeypatch, writer)
    input_started = asyncio.Event()
    input_cancelled = asyncio.Event()

    async def read_input():
        input_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            input_cancelled.set()
            raise

    monkeypatch.setattr(tui, "_read_input_raw", read_input)
    monkeypatch.setattr(tui, "_render_frame", lambda: None)
    monkeypatch.setattr(tui, "_flush_committed", lambda *, force=False: None)
    monkeypatch.setattr(
        tui,
        "_process_input",
        lambda _data: (_ for _ in ()).throw(
            AssertionError("input must not dispatch after writer failure")
        ),
    )

    async def on_submit(_text: str) -> bool:
        return True

    run_task = asyncio.create_task(tui.run(on_submit))
    await asyncio.wait_for(input_started.wait(), timeout=1)
    failure = BrokenPipeError("stdout closed")
    assert writer.on_error is not None
    writer.on_error(failure)

    with pytest.raises(BrokenPipeError) as caught:
        await asyncio.wait_for(run_task, timeout=1)

    assert caught.value is failure
    assert input_cancelled.is_set()
    assert tui._terminal_writer_failed is True
    assert "restore_terminal" in events
    assert "shutdown" in events


@pytest.mark.asyncio
async def test_tty_shutdown_orders_commit_drain_restore_stop_and_dump(
    tmp_path, monkeypatch
):
    events = []
    writer = _LifecycleWriter(events)
    tui = _prepare_lifecycle_tui(tmp_path, monkeypatch, writer)
    flush_count = 0

    def flush_committed(*, force=False):
        nonlocal flush_count
        flush_count += 1
        events.append(("flush_committed", flush_count, force))
        return ("commit", flush_count) if flush_count == 2 else None

    async def read_input():
        return b"\x04"

    monkeypatch.setattr(tui, "_flush_committed", flush_committed)
    monkeypatch.setattr(tui, "_read_input_raw", read_input)

    async def on_submit(_text: str) -> bool:
        return True

    await tui.run(on_submit)

    cleanup_commit = events.index(("flush_committed", 2, True))
    commit_wait = events.index(("wait", "commit"))
    drain = events.index("drain")
    termios_restore = events.index("restore_terminal")
    restore_barrier = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple) and event[:2] == ("barrier", "restore")
    )
    restore_wait = events.index(("wait", "restore"))
    shutdown = events.index("shutdown")
    dump = events.index("dump")
    assert cleanup_commit < commit_wait < drain < termios_restore
    assert termios_restore < restore_barrier < restore_wait < shutdown < dump


@pytest.mark.asyncio
async def test_non_tty_run_does_not_start_terminal_writer(tmp_path, monkeypatch):
    events = []
    writer = _LifecycleWriter(events)
    tui = _tui(tmp_path)
    tui._stdin_fd = None
    tui._terminal_writer = writer

    async def read_input_line():
        return b"\x04"

    monkeypatch.setattr(tui, "_read_input_line", read_input_line)
    monkeypatch.setattr(tui, "_render_frame", lambda: events.append("render"))
    monkeypatch.setattr(tui, "_flush_committed", lambda *, force=False: None)

    async def on_submit(_text: str) -> bool:
        return True

    await tui.run(on_submit)

    assert "writer_start" not in events
    assert "shutdown" not in events
    assert "render" in events


@pytest.mark.asyncio
async def test_writer_failure_wins_when_input_completes_simultaneously(
    tmp_path, monkeypatch
):
    events = []
    writer = _LifecycleWriter(events)
    tui = _prepare_lifecycle_tui(tmp_path, monkeypatch, writer)
    input_started = asyncio.Event()
    release_input = asyncio.Event()

    async def read_input():
        input_started.set()
        await release_input.wait()
        return b"x"

    monkeypatch.setattr(tui, "_read_input_raw", read_input)
    monkeypatch.setattr(tui, "_render_frame", lambda: None)
    monkeypatch.setattr(tui, "_flush_committed", lambda *, force=False: None)
    monkeypatch.setattr(
        tui,
        "_process_input",
        lambda _data: (_ for _ in ()).throw(
            AssertionError("simultaneous input must not dispatch after writer failure")
        ),
    )

    async def on_submit(_text: str) -> bool:
        return True

    run_task = asyncio.create_task(tui.run(on_submit))
    await asyncio.wait_for(input_started.wait(), timeout=1)
    first = OSError("first writer failure")
    second = RuntimeError("later writer failure")
    assert writer.on_error is not None
    writer.on_error(first)
    writer.on_error(second)
    release_input.set()

    with pytest.raises(OSError) as caught:
        await asyncio.wait_for(run_task, timeout=1)

    assert caught.value is first
    assert tui._terminal_writer_failed is True
    assert "restore_terminal" in events
    assert "shutdown" in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_phase",
    ["final_commit", "termios_restore", "restore_barrier", "shutdown"],
)
async def test_normal_tty_exit_propagates_first_cleanup_error_and_continues_cleanup(
    tmp_path, monkeypatch, failure_phase
):
    events = []
    failure = OSError(f"{failure_phase} failed")
    writer = _LifecycleWriter(
        events,
        wait_errors={
            "commit": failure if failure_phase == "final_commit" else None,
            "restore": failure if failure_phase == "restore_barrier" else None,
        },
        shutdown_error=failure if failure_phase == "shutdown" else None,
    )
    tui = _prepare_lifecycle_tui(tmp_path, monkeypatch, writer)
    flush_count = 0

    async def read_input():
        return b"\x04"

    def flush_committed(*, force=False):
        nonlocal flush_count
        flush_count += 1
        events.append(("flush_committed", flush_count, force))
        return ("commit", flush_count) if flush_count == 2 else None

    if failure_phase == "termios_restore":
        def fail_restore_terminal():
            events.append("restore_terminal")
            raise failure

        monkeypatch.setattr(tui, "_restore_terminal", fail_restore_terminal)
    monkeypatch.setattr(tui, "_read_input_raw", read_input)
    monkeypatch.setattr(tui, "_flush_committed", flush_committed)

    async def on_submit(_text: str) -> bool:
        return True

    with pytest.raises(OSError) as caught:
        await tui.run(on_submit)

    assert caught.value is failure
    assert "restore_terminal" in events
    assert "shutdown" in events
    assert dock._refresh_callback is None
    assert dock._width_provider is None
    if failure_phase == "shutdown":
        assert "dump" not in events
    else:
        assert events.index("shutdown") < events.index("dump")


@pytest.mark.asyncio
async def test_normal_tty_exit_preserves_first_cleanup_error(tmp_path, monkeypatch):
    events = []
    first = OSError("final commit failed first")
    later = RuntimeError("termios restore failed later")
    writer = _LifecycleWriter(events, wait_errors={"commit": first})
    tui = _prepare_lifecycle_tui(tmp_path, monkeypatch, writer)
    flush_count = 0

    async def read_input():
        return b"\x04"

    def flush_committed(*, force=False):
        nonlocal flush_count
        flush_count += 1
        return ("commit", flush_count) if flush_count == 2 else None

    def fail_restore_terminal():
        events.append("restore_terminal")
        raise later

    monkeypatch.setattr(tui, "_read_input_raw", read_input)
    monkeypatch.setattr(tui, "_flush_committed", flush_committed)
    monkeypatch.setattr(tui, "_restore_terminal", fail_restore_terminal)

    async def on_submit(_text: str) -> bool:
        return True

    with pytest.raises(OSError) as caught:
        await tui.run(on_submit)

    assert caught.value is first
    assert "shutdown" in events
    assert "dump" in events


@pytest.mark.asyncio
async def test_run_cancellation_during_consumer_cleanup_is_deferred_and_propagated(
    tmp_path, monkeypatch
):
    events = []
    writer = _LifecycleWriter(events)
    tui = _prepare_lifecycle_tui(tmp_path, monkeypatch, writer)
    consumer_cleanup_started = asyncio.Event()
    release_consumer = asyncio.Event()

    async def consume(_on_submit):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            consumer_cleanup_started.set()
            await release_consumer.wait()

    async def read_input():
        tui._running = False
        return b""

    monkeypatch.setattr(tui, "_consume", consume)
    monkeypatch.setattr(tui, "_read_input_raw", read_input)
    monkeypatch.setattr(tui, "_flush_committed", lambda *, force=False: None)
    monkeypatch.setattr(tui, "_render_frame", lambda: None)

    async def on_submit(_text: str) -> bool:
        return True

    run_task = asyncio.create_task(tui.run(on_submit))
    try:
        await asyncio.wait_for(consumer_cleanup_started.wait(), timeout=1)
        run_task.cancel()
        done, _ = await asyncio.wait({run_task}, timeout=0.05)
        assert done == set()

        release_consumer.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=1)
    finally:
        release_consumer.set()
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    assert "restore_terminal" in events
    assert "shutdown" in events
    assert dock._refresh_callback is None
    assert dock._width_provider is None


@pytest.mark.asyncio
async def test_run_cancellation_during_transcript_waits_for_export_then_propagates(
    tmp_path, monkeypatch
):
    import voidx_cli.app as app_module

    events = []
    writer = _LifecycleWriter(events)
    tui = _prepare_lifecycle_tui(tmp_path, monkeypatch, writer)
    export_started = threading.Event()
    release_export = threading.Event()

    def blocking_dump(*args, **kwargs):
        del args, kwargs
        events.append("dump_started")
        export_started.set()
        if not release_export.wait(timeout=2):
            raise TimeoutError("transcript export was not released")
        events.append("dump_finished")

    async def read_input():
        tui._running = False
        return b""

    monkeypatch.setattr(app_module, "_dump_transcript_log", blocking_dump)
    monkeypatch.setattr(tui, "_read_input_raw", read_input)
    monkeypatch.setattr(tui, "_flush_committed", lambda *, force=False: None)
    monkeypatch.setattr(tui, "_render_frame", lambda: None)

    async def on_submit(_text: str) -> bool:
        return True

    run_task = asyncio.create_task(tui.run(on_submit))
    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(export_started.wait, 1),
            timeout=2,
        )
        run_task.cancel()
        done, _ = await asyncio.wait({run_task}, timeout=0.05)
        assert done == set()

        release_export.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=1)
    finally:
        release_export.set()
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    assert events.index("dump_started") < events.index("dump_finished")
    assert dock._refresh_callback is None
    assert dock._width_provider is None
