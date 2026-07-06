from tui_helpers import *  # noqa: F403

import sys
import types

import pytest


class _FakeMsvcrt:
    """Fake msvcrt module with a scripted character queue."""

    def __init__(self, chars: list[str]) -> None:
        self._chars = list(chars)
        self._consumed: list[str] = []

    def kbhit(self) -> bool:
        return bool(self._chars)

    def getwch(self) -> str:
        if not self._chars:
            raise AssertionError("getwch called on empty buffer")
        ch = self._chars.pop(0)
        self._consumed.append(ch)
        return ch


def _install_fake_msvcrt(monkeypatch, chars: list[str]) -> _FakeMsvcrt:
    fake = _FakeMsvcrt(chars)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    return fake


# ── _try_drain_win32_paste ─────────────────────────────────────────────


def test_drain_multiline_paste_returns_bracketed_sequence(tmp_path, monkeypatch):
    """Multi-line paste: \\r\\n followed by more content → bracketed paste."""
    _install_fake_msvcrt(monkeypatch, ["\n", "l", "i", "n", "e", "2"])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=50)

    assert result is not None
    assert result.startswith(b"\x1b[200~")
    assert result.endswith(b"\x1b[201~")
    # Content between markers should contain the drained characters
    content = result[len(b"\x1b[200~"):-len(b"\x1b[201~")]
    assert content == "\r\nline2".encode("utf-8")


def test_drain_single_enter_returns_none(tmp_path, monkeypatch):
    """Single Enter with no follow-up → None (not a paste)."""
    _install_fake_msvcrt(monkeypatch, [])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=10)

    assert result is None


def test_drain_two_newlines_returns_bracketed_sequence(tmp_path, monkeypatch):
    """Two \\r in quick succession (newline_count >= 2) → paste."""
    _install_fake_msvcrt(monkeypatch, ["\r", "x"])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=50)

    assert result is not None
    content = result[len(b"\x1b[200~"):-len(b"\x1b[201~")]
    assert content == "\r\rx".encode("utf-8")


def test_drain_long_single_line_returns_bracketed_sequence(tmp_path, monkeypatch):
    """Single-line but long (>8 chars) → paste."""
    _install_fake_msvcrt(monkeypatch, list("abcdefghij"))
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=50)

    assert result is not None
    content = result[len(b"\x1b[200~"):-len(b"\x1b[201~")]
    assert content == "\rabcdefghij".encode("utf-8")


def test_drain_short_followup_after_enter_returns_raw(tmp_path, monkeypatch):
    """Enter followed by 1-2 chars (not enough) → raw bytes, not a paste.

    Plan B: non-paste drained content is returned as raw bytes (all chars
    in arrival order) instead of None + _pending_bytes stashing.
    """
    _install_fake_msvcrt(monkeypatch, ["x"])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=50)

    # Not a paste (1 newline, 2 chars) → raw bytes of all drained chars
    assert result == b"\rx"


def test_drain_non_newline_first_char_returns_raw(tmp_path, monkeypatch):
    """First char not \\r/\\n with follow-up → raw bytes (not paste).

    Plan B: non-paste drained content is returned as raw bytes instead
    of None + _pending_bytes stashing.
    """
    _install_fake_msvcrt(monkeypatch, ["x", "y", "z"])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("a", timeout_ms=50)

    # 'a' + 'xyz' = 4 chars, 0 newlines → not paste → raw bytes
    assert result == b"axyz"


def test_drain_function_key_prefix_consumes_second_byte(tmp_path, monkeypatch):
    """Drain encountering 0x00/0xe0 must consume the second byte and stop."""
    # Multi-line paste that ends with a function key prefix
    _install_fake_msvcrt(monkeypatch, ["\n", "a", "b", "\x00", "H"])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=50)

    assert result is not None
    content = result[len(b"\x1b[200~"):-len(b"\x1b[201~")]
    # \x00 and 'H' should be consumed but NOT included in paste content
    assert content == "\r\nab".encode("utf-8")


def test_drain_e0_prefix_consumes_second_byte(tmp_path, monkeypatch):
    """Drain encountering 0xe0 must consume the second byte and stop."""
    _install_fake_msvcrt(monkeypatch, ["\n", "x", "\xe0", "K"])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=50)

    assert result is not None
    content = result[len(b"\x1b[200~"):-len(b"\x1b[201~")]
    assert content == "\r\nx".encode("utf-8")


