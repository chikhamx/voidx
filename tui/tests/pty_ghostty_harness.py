"""Real-PTY harness driving a real PureTui child process through a VT model.

Child runs in a forked pty with TERM_PROGRAM=ghostty, builds committed history
plus an active subagent + Wait via the dock event consumer, and renders through
the full raw-termios / worker-writer / synchronized-output path. The parent
feeds output bytes into a VT screen model and applies Ghostty-modeled resizes
via TIOCSWINSZ, using bracketed-paste key sequences as deterministic triggers.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pathlib
import pty
import signal
import struct
import sys
import termios

import pytest

from test_layout_terminal_model import _VTScreen

WIDTH = 80
START_HEIGHT = 24
# Private paste-wrapped keys: distinct, printable, never produced by the TUI.
RESIZE_KEYS = {20: "A", 16: "B", 11: "C", 24: "D"}
QUERY_KEY = "Q"
EXIT_KEY = "E"
PASTE_BEGIN = b"\x1b[200~"
PASTE_END = b"\x1b[201~"

_CHILD_RUNNER = str(
    (pathlib.Path(__file__).parent / "pty_child_runner.py").resolve()
)


class _GhosttyScreen(_VTScreen):
    """Accept DEC private mode set/reset (h/l), which the real TUI emits for
    mouse reporting, bracketed paste, cursor visibility, etc. They do not
    affect cell contents."""

    def _csi(self, sequence: str):
        if len(sequence) > 2 and sequence[2] == "?" and sequence[-1] in "hl":
            return
        super()._csi(sequence)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _ghostty_shrink(screen: _VTScreen, height: int) -> tuple:
    """Ghostty PageList.resizeWithoutReflow: trim text-free unpinned tail,
    then push the overflow top rows into scrollback. Returns history after."""
    removed = screen.height - height
    trimmed = 0
    while trimmed < removed:
        row = screen.height - trimmed - 1
        if row == screen._row or any(
            cell not in (None, " ") for cell in screen._cells[row]
        ):
            break
        trimmed += 1
    pushed = removed - trimmed
    screen.scrollback.extend(tuple(r) for r in screen._cells[:pushed])
    screen._cells = screen._cells[pushed:pushed + height]
    screen._row = max(0, screen._row - pushed)
    screen.height = height
    screen._top_margin, screen._bottom_margin = 0, height - 1
    screen._wrap_pending = False
    screen._displayed_cells = screen.cells
    return screen.history


class PtySession:
    def __init__(self, tmp_path):
        if sys.platform == "win32":
            pytest.skip("pty coverage is POSIX-only")
        self._tmp_path = tmp_path
        self.screen = _GhosttyScreen(width=WIDTH, height=START_HEIGHT)
        self.errors: list[str] = []
        self.pid: int | None = None
        self._fd: int | None = None

    def __enter__(self) -> "PtySession":
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM_PROGRAM"] = "ghostty"
            os.chdir(self._tmp_path)
            os.execl(sys.executable, sys.executable, _CHILD_RUNNER)
        self.pid = pid
        self._fd = fd
        _set_winsize(fd, START_HEIGHT, WIDTH)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                os.waitpid(self.pid, 0)
            except (ChildProcessError, OSError):
                pass
        return False

    def send_key(self, char: str) -> None:
        os.write(self._fd, PASTE_BEGIN + char.encode() + PASTE_END)

    def send_resize(self, height: int) -> None:
        _set_winsize(self._fd, height, WIDTH)
        self.send_key(RESIZE_KEYS[height])

    async def settle(self, timeout: float = 15.0) -> None:
        """Read until output goes quiet and the writer is idle (stream ends
        outside any synchronized-output block). Always drains pending bytes."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_data = loop.time()
        while loop.time() < deadline:
            got = False
            while True:
                try:
                    data = os.read(self._fd, 65536)
                except BlockingIOError:
                    break
                except OSError:
                    data = b""
                    break
                if not data:
                    break
                got = True
                text = data.decode("utf-8", "replace")
                if "CHILD-ERROR" in text:
                    self.errors.append(text)
                self.screen.feed(text)
            if got:
                last_data = loop.time()
                continue
            if loop.time() - last_data > 0.3 and not self.screen.synchronized:
                return
            await asyncio.sleep(0.02)
        raise TimeoutError(f"pty session did not settle; errors={self.errors}")
