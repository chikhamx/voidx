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


# ── _read_input_raw_win32 integration ──────────────────────────────────


@pytest.mark.asyncio
async def test_read_input_raw_win32_multiline_paste_produces_bracketed(tmp_path, monkeypatch):
    """Full _read_input_raw_win32: first char \\r triggers drain → bracketed paste."""
    _install_fake_msvcrt(monkeypatch, ["\r", "\n", "l", "i", "n", "e", "2"])
    tui = _tui(tmp_path)

    result = await tui._read_input_raw_win32()

    assert result.startswith(b"\x1b[200~")
    assert result.endswith(b"\x1b[201~")


@pytest.mark.asyncio
async def test_read_input_raw_win32_single_enter_returns_cr(tmp_path, monkeypatch):
    """Full _read_input_raw_win32: single Enter with no follow-up → raw \\r."""
    _install_fake_msvcrt(monkeypatch, ["\r"])
    tui = _tui(tmp_path)

    result = await tui._read_input_raw_win32()

    assert result == b"\r"


@pytest.mark.asyncio
async def test_read_input_raw_win32_regular_char_unchanged(tmp_path, monkeypatch):
    """Full _read_input_raw_win32: regular char → unchanged."""
    _install_fake_msvcrt(monkeypatch, ["a"])
    tui = _tui(tmp_path)

    result = await tui._read_input_raw_win32()

    assert result == b"a"


@pytest.mark.asyncio
async def test_read_input_raw_win32_function_key_unchanged(tmp_path, monkeypatch):
    """Full _read_input_raw_win32: function key (0x00 prefix) → mapped escape."""
    _install_fake_msvcrt(monkeypatch, ["\x00", "H"])
    tui = _tui(tmp_path)

    # First char is 0x00, second is 'H' (Up arrow)
    result = await tui._read_input_raw_win32()

    assert result == b"\x1b[A"


# ── End-to-end: drain → _process_input → _insert_pasted_text ──────────


@pytest.mark.asyncio
async def test_e2e_multiline_paste_preserves_all_lines(tmp_path, monkeypatch):
    """E2E: drain produces bracketed paste → _process_input inserts all lines."""
    # Simulate Windows Terminal injecting "line1\r\nline2\r\nline3"
    # First char 'l' is read by getwch, then \r triggers drain
    _install_fake_msvcrt(monkeypatch, ["\r", "\n", "l", "i", "n", "e", "2", "\r", "\n", "l", "i", "n", "e", "3"])
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    # Read raw input (first char 'l' — not a newline, so no drain)
    # We need to simulate the full flow: first 'l','i','n','e','1' are read
    # one at a time, then '\r' triggers drain.
    # For E2E test, let's start with \r as first char (empty first line paste)
    _install_fake_msvcrt(monkeypatch, ["\n", "l", "i", "n", "e", "1", "\r", "\n", "l", "i", "n", "e", "2"])
    raw = await tui._read_input_raw_win32()

    # Should be a bracketed paste sequence
    assert raw.startswith(b"\x1b[200~")
    assert raw.endswith(b"\x1b[201~")

    # Process it through _process_input
    tui._process_input(raw)

    # All lines should be collapsed into a paste token
    assert tui._get_input_text() == "[Pasted text #1 +2 lines]"
    assert tui._paste_entries[0]["expanded"] == "\nline1\nline2"
    assert tui._queue.empty()


@pytest.mark.asyncio
async def test_e2e_large_paste_collapses_to_token(tmp_path, monkeypatch):
    """E2E: large paste (>3 lines) collapses to [Pasted text #N +M lines] token."""
    # Simulate a 5-line paste starting with \r
    chars = list("\nline1\r\nline2\r\nline3\r\nline4\r\nline5")
    _install_fake_msvcrt(monkeypatch, chars)
    tui = _tui(tmp_path)
    tui._tty = True

    raw = await tui._read_input_raw_win32()
    assert raw.startswith(b"\x1b[200~")

    tui._process_input(raw)

    # Should collapse to token (6 lines: empty + 5 lines > 3)
    text = tui._get_input_text()
    assert "[Pasted text #1" in text
    assert tui._queue.empty()


@pytest.mark.asyncio
async def test_e2e_single_enter_does_not_submit_prematurely(tmp_path, monkeypatch):
    """E2E: single Enter with no follow-up → raw \r → submit (not paste)."""
    _install_fake_msvcrt(monkeypatch, ["\r"])
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    raw = await tui._read_input_raw_win32()
    assert raw == b"\r"

    tui._process_input(raw)

    # Should submit, not paste
    assert not tui._queue.empty()
    assert tui._queue.get_nowait() == "hello"


