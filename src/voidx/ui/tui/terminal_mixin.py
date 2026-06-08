"""Terminal setup and restore support for PureTui."""

from __future__ import annotations

import os
import sys as _sys

_STD_OUTPUT_HANDLE = -11
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

if _sys.platform == "win32":
    termios = None  # type: ignore[assignment]
else:
    import termios  # type: ignore[no-redef]


def _enable_windows_virtual_terminal_processing(kernel32: object | None = None) -> int | None:
    """Enable ANSI escape processing on Windows stdout.

    Returns the original console mode when stdout is a console and mode was read.
    Non-console stdout, unsupported APIs, and SetConsoleMode failures are silent
    no-ops so redirected output and legacy shells can still run.
    """
    try:
        import ctypes

        if kernel32 is None:
            kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        original = int(mode.value)
        updated = original | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if updated == original:
            return original
        if not kernel32.SetConsoleMode(handle, updated):
            return None
        return original
    except Exception:
        return None


def _restore_windows_console_mode(
    original_mode: int | None,
    kernel32: object | None = None,
) -> bool:
    if original_mode is None:
        return False
    try:
        import ctypes

        if kernel32 is None:
            kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        return bool(kernel32.SetConsoleMode(handle, int(original_mode)))
    except Exception:
        return False


class _TerminalLifecycleMixin:
    def _setup_terminal(self) -> None:
        if self._stdin_fd is None or not os.isatty(self._stdin_fd):
            return
        if termios is not None:
            self._old_termios = termios.tcgetattr(self._stdin_fd)
            new = termios.tcgetattr(self._stdin_fd)
            # raw mode: no echo, no canonical, no CR->LF translation, VMIN=1 VTIME=0
            # VMIN=1: raw reads wait for at least 1 byte, while asyncio
            # drains the available terminal bytes into the StreamReader.
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
            self._windows_stdout_mode = _enable_windows_virtual_terminal_processing()

    def _restore_terminal(self) -> None:
        if termios is not None and self._old_termios is not None:
            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._old_termios)
            self._old_termios = None
            return
        if self._windows_stdout_mode is not None:
            _restore_windows_console_mode(self._windows_stdout_mode)
            self._windows_stdout_mode = None
