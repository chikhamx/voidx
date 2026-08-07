"""Integrated terminal PTY manager for the v2 gateway.

Provides cross-platform PTY management:
- Windows: pywinpty
- Unix: os.forkpty / pty module

Each TerminalSession wraps a PTY process and exposes async read/write/resize.
TerminalManager tracks all active sessions by terminal_id.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from voidx.logging import log_internal_error

_IS_WINDOWS = sys.platform == "win32"


@dataclass
class TerminalOutput:
    """A chunk of output from a terminal session."""

    terminal_id: str
    data: str

    def to_notification_params(self) -> dict:
        return {"terminal_id": self.terminal_id, "data": self.data}


@dataclass
class TerminalSession:
    """Wraps a single PTY process."""

    terminal_id: str
    _pty: object
    pid: int = 0
    cols: int = 80
    rows: int = 25
    _closed: bool = False

    async def write(self, data: str) -> None:
        """Send input to the PTY."""
        if self._closed:
            return
        if _IS_WINDOWS:
            await asyncio.to_thread(self._pty.write, data)
        else:
            os.write(self._pty.fd, data.encode())

    async def resize(self, *, cols: int, rows: int) -> None:
        """Adjust PTY dimensions."""
        self.cols = cols
        self.rows = rows
        if self._closed:
            return
        if _IS_WINDOWS:
            await asyncio.to_thread(self._pty.set_size, cols, rows)
        else:
            import struct
            import fcntl
            import termios
            fcntl.ioctl(
                self._pty.fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )

    async def read(self) -> AsyncIterator[str]:
        """Yield output chunks from the PTY until it exits."""
        while not self._closed:
            if _IS_WINDOWS:
                data = await asyncio.to_thread(self._read_winpty)
            else:
                data = await asyncio.to_thread(self._read_unix)
            if data:
                yield data
                continue
            # No data available — check if process exited
            if self._is_exited():
                # Drain remaining output
                if _IS_WINDOWS:
                    remaining = await asyncio.to_thread(self._read_winpty)
                else:
                    remaining = await asyncio.to_thread(self._read_unix)
                if remaining:
                    yield remaining
                break
            await asyncio.sleep(0.05)

    def _read_winpty(self) -> str:
        try:
            return self._pty.read()
        except Exception:
            return ""

    def _read_unix(self) -> str:
        try:
            return os.read(self._pty.fd, 65536).decode(errors="replace")
        except OSError:
            return ""

    def _is_exited(self) -> bool:
        if _IS_WINDOWS:
            return not self._pty.isalive()
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            return pid != 0
        except ChildProcessError:
            return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if _IS_WINDOWS:
            try:
                self._pty.cancel_io()
            except Exception as exc:
                log_internal_error(exc, context="gateway_pty_cancel_io_windows")
            self._pty = None
        else:
            try:
                os.close(self._pty.fd)
            except OSError:
                pass


class TerminalManager:
    """Manages multiple PTY terminal sessions."""

    def __init__(self) -> None:
        self.sessions: dict[str, TerminalSession] = {}

    async def create(
        self,
        command: list[str],
        *,
        cols: int = 80,
        rows: int = 25,
        cwd: str | None = None,
    ) -> TerminalSession:
        terminal_id = uuid.uuid4().hex[:12]
        if _IS_WINDOWS:
            pty = await asyncio.to_thread(self._create_winpty, command, cols, rows, cwd)
        else:
            pty, pid = await asyncio.to_thread(self._create_unix_pty, command, cols, rows, cwd)
        session = TerminalSession(
            terminal_id=terminal_id,
            _pty=pty,
            pid=pty.pid if _IS_WINDOWS else pid,
            cols=cols,
            rows=rows,
        )
        self.sessions[terminal_id] = session
        return session

    def _create_winpty(self, command: list[str], cols: int, rows: int, cwd: str | None):
        from winpty import PTY
        pty = PTY(cols, rows)
        cmd_str = " ".join(f'"{a}"' if " " in a else a for a in command)
        if cwd:
            pty.spawn(cmd_str, cwd=cwd)
        else:
            pty.spawn(cmd_str)
        return pty

    def _create_unix_pty(self, command: list[str], cols: int, rows: int, cwd: str | None):
        import fcntl
        import pty
        import struct
        import subprocess
        import termios

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        process = subprocess.Popen(
            command,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)

        class _UnixPty:
            fd = master

        return _UnixPty(), process.pid

    def get(self, terminal_id: str) -> TerminalSession | None:
        return self.sessions.get(terminal_id)

    async def close(self, terminal_id: str) -> None:
        session = self.sessions.pop(terminal_id, None)
        if session is not None:
            await asyncio.to_thread(session.close)

    async def close_all(self) -> None:
        ids = list(self.sessions.keys())
        for terminal_id in ids:
            await self.close(terminal_id)
