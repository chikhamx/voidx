"""Raw keyboard input — cross-platform, no-Enter key capture."""

from __future__ import annotations

import select
import sys
from enum import Enum
from typing import Any


class Key(Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    ENTER = "enter"
    ESC = "esc"
    TAB = "tab"
    BACKSPACE = "backspace"
    CTRL_C = "ctrl_c"
    CTRL_D = "ctrl_d"
    CTRL_L = "ctrl_l"
    BRACKETED_PASTE_START = "bracketed_paste_start"
    BRACKETED_PASTE_END = "bracketed_paste_end"
    CHAR = "char"


class KeyEvent:
    def __init__(self, key: Key, char: str = ""):
        self.key = key
        self.char = char

    @property
    def is_char(self) -> bool:
        return self.key == Key.CHAR

    @property
    def is_enter(self) -> bool:
        return self.key == Key.ENTER

    @property
    def is_esc(self) -> bool:
        return self.key == Key.ESC

    @property
    def is_paste_start(self) -> bool:
        return self.key == Key.BRACKETED_PASTE_START

    @property
    def is_paste_end(self) -> bool:
        return self.key == Key.BRACKETED_PASTE_END

    def __repr__(self) -> str:
        return f"KeyEvent({self.key.value}, {self.char!r})"


def enable_bracketed_paste() -> None:
    """Enable terminal bracketed paste mode.

    When enabled, the terminal wraps pasted text with
    ``\\x1b[200~`` (start) and ``\\x1b[201~`` (end) sequences,
    allowing the application to detect and handle paste events.
    """
    sys.stdout.write("\x1b[?2004h")
    sys.stdout.flush()


def disable_bracketed_paste() -> None:
    """Disable terminal bracketed paste mode."""
    sys.stdout.write("\x1b[?2004l")
    sys.stdout.flush()


def read_key() -> KeyEvent:
    """Read a single keypress. Blocks until a key is pressed.

    Returns a KeyEvent with the key type and character (if applicable).
    Works on Windows (msvcrt) and Unix (termios).
    """
    if sys.platform == "win32":
        return _read_key_windows()
    else:
        return _read_key_unix()


def _read_key_windows() -> KeyEvent:
    import msvcrt

    while True:
        ch = msvcrt.getwch()
        # Ctrl-C
        if ch == "\x03":
            return KeyEvent(Key.CTRL_C)
        # Ctrl-D
        if ch == "\x04":
            return KeyEvent(Key.CTRL_D)
        # Ctrl-L
        if ch == "\x0c":
            return KeyEvent(Key.CTRL_L)
        # Enter
        if ch == "\r":
            return KeyEvent(Key.ENTER)
        # Backspace
        if ch == "\x08" or ch == "\x7f":
            return KeyEvent(Key.BACKSPACE)
        # Tab
        if ch == "\t":
            return KeyEvent(Key.TAB)
        # Extended keys (arrows, etc.) — \x00 or \xe0 prefix
        if ch == "\x00" or ch == "\xe0":
            ch2 = msvcrt.getwch()
            arrows = {"H": Key.UP, "P": Key.DOWN, "K": Key.LEFT, "M": Key.RIGHT}
            return KeyEvent(arrows.get(ch2, Key.CHAR), ch2)
        # Escape — possibly start of a CSI sequence
        if ch == "\x1b":
            if not msvcrt.kbhit():
                return KeyEvent(Key.ESC)
            ch2 = msvcrt.getwch()
            if ch2 == "[":
                # CSI sequence — read until a final byte (0x40–0x7E)
                params: list[str] = []
                while True:
                    ch3 = msvcrt.getwch()
                    if "\x40" <= ch3 <= "\x7e":
                        params.append(ch3)
                        break
                    params.append(ch3)
                param_str = "".join(params)
                if param_str == "200~":
                    return KeyEvent(Key.BRACKETED_PASTE_START)
                if param_str == "201~":
                    return KeyEvent(Key.BRACKETED_PASTE_END)
                arrows = {"A": Key.UP, "B": Key.DOWN, "C": Key.RIGHT, "D": Key.LEFT}
                if param_str in arrows:
                    return KeyEvent(arrows[param_str])
                return KeyEvent(Key.ESC)
            elif ch2 == "O":
                ch3 = msvcrt.getwch()
                arrows = {"A": Key.UP, "B": Key.DOWN, "C": Key.RIGHT, "D": Key.LEFT}
                if ch3 in arrows:
                    return KeyEvent(arrows[ch3])
                return KeyEvent(Key.ESC)
            else:
                return KeyEvent(Key.ESC)
        # Regular character
        if ch.isprintable() or ch in "\n":
            return KeyEvent(Key.CHAR, ch)
        return KeyEvent(Key.CHAR, ch)


def _read_key_unix() -> KeyEvent:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.buffer.read(1)
        # Ctrl-C
        if ch == b"\x03":
            return KeyEvent(Key.CTRL_C)
        # Ctrl-D
        if ch == b"\x04":
            return KeyEvent(Key.CTRL_D)
        # Ctrl-L
        if ch == b"\x0c":
            return KeyEvent(Key.CTRL_L)
        # Enter
        if ch == b"\r":
            return KeyEvent(Key.ENTER)
        # Backspace / DEL
        if ch == b"\x7f" or ch == b"\x08":
            return KeyEvent(Key.BACKSPACE)
        # Tab
        if ch == b"\t":
            return KeyEvent(Key.TAB)
        # Escape — possibly start of a CSI sequence
        if ch == b"\x1b":
            # Use select to check if more bytes are available
            r, _, _ = select.select([sys.stdin.buffer], [], [], 0.01)
            if not r:
                return KeyEvent(Key.ESC)
            ch2 = sys.stdin.buffer.read(1)
            if ch2 == b"[":
                # CSI sequence — read until a final byte (0x40–0x7E)
                params: list[bytes] = []
                while True:
                    b = sys.stdin.buffer.read(1)
                    if b"\x40" <= b <= b"\x7e":
                        params.append(b)
                        break
                    params.append(b)
                param_str = b"".join(params).decode("ascii", errors="replace")
                if param_str == "200~":
                    return KeyEvent(Key.BRACKETED_PASTE_START)
                if param_str == "201~":
                    return KeyEvent(Key.BRACKETED_PASTE_END)
                arrows = {"A": Key.UP, "B": Key.DOWN, "C": Key.RIGHT, "D": Key.LEFT}
                if param_str in arrows:
                    return KeyEvent(arrows[param_str])
                return KeyEvent(Key.ESC)
            elif ch2 == b"O":
                b = sys.stdin.buffer.read(1)
                arrows = {b"A": Key.UP, b"B": Key.DOWN, b"C": Key.RIGHT, b"D": Key.LEFT}
                if b in arrows:
                    return KeyEvent(arrows[b])
                return KeyEvent(Key.ESC)
            else:
                return KeyEvent(Key.ESC)
        # Regular character
        return KeyEvent(Key.CHAR, ch.decode("utf-8", errors="replace"))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)