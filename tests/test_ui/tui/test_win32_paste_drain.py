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


def test_drain_short_followup_after_enter_returns_none(tmp_path, monkeypatch):
    """Enter followed by 1-2 chars (not enough) → None (not a paste)."""
    _install_fake_msvcrt(monkeypatch, ["x"])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=50)

    assert result is None


def test_drain_non_newline_first_char_returns_none(tmp_path, monkeypatch):
    """First char not \\r/\\n → immediately None, no drain."""
    _install_fake_msvcrt(monkeypatch, ["x", "y", "z"])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("a", timeout_ms=50)

    assert result is None


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


# ── R3: function key prefix in paste stream ───────────────────────────


def test_r3_function_key_prefix_in_paste_stream(tmp_path, monkeypatch):
    """R3: paste content starting with \\x00/\\xe0 → drain breaks, no data loss."""
    # First char \r (newline), then \x00 + 'H' (function key), then more content
    _install_fake_msvcrt(monkeypatch, ["\x00", "H", "\n", "x", "y", "z"])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=50)

    # \x00 causes break, so buffer = "\r" only (1 char, 1 newline)
    # newline_count=1 < 2, len=1 <= 8 → not a paste → None
    # But the function key bytes are consumed, no ghost keypress
    assert result is None  # Not enough content after break to be a paste


def test_r3_function_key_prefix_with_enough_content(tmp_path, monkeypatch):
    """R3: function key prefix after enough content → paste with content before prefix."""
    # \r + enough content + \x00 + 'H' (function key breaks drain)
    _install_fake_msvcrt(monkeypatch, ["\n", "a", "b", "c", "d", "e", "f", "g", "\x00", "H"])
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=50)

    assert result is not None
    content = result[len(b"\x1b[200~"):-len(b"\x1b[201~")]
    # Content includes everything up to (but not including) the function key
    assert content == "\r\nabcdefg".encode("utf-8")
    # \x00 and 'H' are consumed, not in content
    assert b"\x00" not in content
    assert b"H" not in content


# ── R1: 分批灌入时序模拟 ─────────────────────────────────────────────


class _TimedFakeMsvcrt:
    """Fake msvcrt that simulates character arrival timing.

    ``schedule`` is a list of (char, delay_ms) tuples. Each character
    becomes available after ``delay_ms`` milliseconds have elapsed since
    the previous character. ``kbhit()`` returns True only when the next
    character's delay has elapsed.
    """

    def __init__(self, schedule: list[tuple[str, float]]) -> None:
        self._schedule = list(schedule)
        self._t0 = None
        self._elapsed = 0.0
        self._consumed: list[str] = []

    def _tick(self) -> float:
        import time
        if self._t0 is None:
            self._t0 = time.monotonic()
        return (time.monotonic() - self._t0) * 1000.0

    def kbhit(self) -> bool:
        if not self._schedule:
            return False
        return self._tick() >= self._schedule[0][1]

    def getwch(self) -> str:
        if not self._schedule:
            raise AssertionError("getwch called on empty schedule")
        ch, _delay = self._schedule.pop(0)
        self._consumed.append(ch)
        return ch


def _install_timed_msvcrt(monkeypatch, schedule: list[tuple[str, float]]) -> _TimedFakeMsvcrt:
    fake = _TimedFakeMsvcrt(schedule)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    return fake


def test_r1_fast_paste_within_timeout(tmp_path, monkeypatch):
    """R1: all chars arrive within 20ms → paste detected."""
    # 10 chars, all arriving at 0-5ms (fast paste)
    schedule = [(ch, i * 0.5) for i, ch in enumerate("\nline2\r\nli")]
    _install_timed_msvcrt(monkeypatch, schedule)
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=20)

    assert result is not None
    assert result.startswith(b"\x1b[200~")
    assert result.endswith(b"\x1b[201~")


def test_r1_split_batch_first_batch_detected(tmp_path, monkeypatch):
    """R1: first batch arrives fast, second batch after 30ms gap.

    With 20ms timeout, drain captures only the first batch.
    The first batch has enough newlines (>=2) to be detected as paste.
    """
    # First batch: \r\nline2\r\n (arrives at 0-3ms)
    # Gap: 30ms
    # Second batch: line3\r\n (arrives at 33-36ms)
    schedule = [
        ("\n", 0), ("l", 0.5), ("i", 1), ("n", 1.5), ("e", 2), ("2", 2.5),
        ("\r", 3), ("\n", 3.5),
        # 30ms gap — these won't be read within 20ms timeout
        ("l", 33), ("i", 33.5), ("n", 34), ("e", 34.5), ("3", 35), ("\r", 35.5), ("\n", 36),
    ]
    _install_timed_msvcrt(monkeypatch, schedule)
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=20)

    # First batch has \r (first_char) + \n + ... + \r + \n = 4 newlines >= 2
    assert result is not None
    content = result[len(b"\x1b[200~"):-len(b"\x1b[201~")]
    # Only first batch content (up to the gap)
    assert content == "\r\nline2\r\n".encode("utf-8")
    # Second batch content NOT included
    assert b"line3" not in content


def test_r1_split_batch_first_batch_too_short(tmp_path, monkeypatch):
    """R1: first batch too short (<2 newlines, <=8 chars) → not detected as paste.

    This is the dangerous case: first batch has only 1 newline, drain returns None,
    and the \r is treated as submit. The second batch's content is lost.
    """
    # First batch: \n + 3 chars (arrives at 0-2ms) — only 1 newline (first_char \r + \n = 2)
    # Actually first_char=\r, drain reads \n + "abc" → buffer=\r\nabc, newline_count=2 >= 2
    # Let's make first batch truly insufficient: \n only (1 char after first_char)
    # buffer = \r + \n = 2 newlines, 2 chars → newline_count=2 >= 2 → paste!
    # To get <2 newlines, first batch must have 0 additional newlines:
    schedule = [
        ("a", 0), ("b", 0.5),  # \r + "ab" = 1 newline, 3 chars → not paste
        # 30ms gap
        ("\n", 33), ("x", 33.5),
    ]
    _install_timed_msvcrt(monkeypatch, schedule)
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=20)

    # buffer = "\r" + "ab" = 1 newline, 3 chars → not paste
    assert result is None
    # This means \r is treated as submit, and "ab" + later "\nx" are lost
    # This is the R1 risk: split-batch with insufficient first batch


# ── R4: 快速连按 Enter 时序模拟 ───────────────────────────────────────


def test_r4_double_enter_within_20ms_detected_as_paste(tmp_path, monkeypatch):
    """R4: two \\r within 20ms → second \\r drained, first submit swallowed."""
    # First \r is first_char, second \r arrives at 10ms
    schedule = [("\r", 10)]
    _install_timed_msvcrt(monkeypatch, schedule)
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=20)

    # buffer = "\r" + "\r" = 2 newlines >= 2 → paste (false positive!)
    assert result is not None
    content = result[len(b"\x1b[200~"):-len(b"\x1b[201~")]
    assert content == b"\r\r"


def test_r4_double_enter_beyond_20ms_not_detected(tmp_path, monkeypatch):
    """R4: two \\r with >20ms gap → second \\r not drained, submit preserved."""
    # First \r is first_char, second \r arrives at 25ms (after timeout)
    schedule = [("\r", 25)]
    _install_timed_msvcrt(monkeypatch, schedule)
    tui = _tui(tmp_path)

    result = tui._try_drain_win32_paste("\r", timeout_ms=20)

    # Second \r arrives after timeout → not drained → None
    assert result is None
