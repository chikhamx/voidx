"""Terminal setup and restore support for PureTui."""

from __future__ import annotations

import os
import sys as _sys

if _sys.platform == "win32":
    termios = None  # type: ignore[assignment]
else:
    import termios  # type: ignore[no-redef]


class _TerminalLifecycleMixin:
    def _setup_terminal(self) -> None:
        if self._stdin_fd is None or not os.isatty(self._stdin_fd):
            return
        if termios is not None:
            self._old_termios = termios.tcgetattr(self._stdin_fd)
            new = termios.tcgetattr(self._stdin_fd)
            # raw mode: no echo, no canonical, no CR->LF translation, VMIN=1 VTIME=0
            # VMIN=1: os.read() blocks until at least 1 byte, then returns
            #          ALL available bytes (escape sequences arrive as one burst)
            new[3] = new[3] & ~(
                termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN
            )
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            # Disable VLNEXT (Ctrl+V literal-next) so 0x16 reaches os.read()
            if hasattr(termios, "VLNEXT"):
                new[6][termios.VLNEXT] = 0
            # Keep BRKINT so Ctrl+C sends SIGINT as fallback
            new[0] = new[0] & ~(termios.IGNBRK | termios.ICRNL)
            new[0] |= termios.BRKINT
            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, new)
        else:
            # Windows: no termios, msvcrt handles raw reads directly
            self._old_termios = None

    def _restore_terminal(self) -> None:
        if termios is not None and self._old_termios is not None:
            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._old_termios)

