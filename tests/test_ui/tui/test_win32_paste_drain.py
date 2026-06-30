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
    # newline_count=1 < 2, len=1 <= 8 → not a paste → raw bytes
    # But the function key bytes are consumed, no ghost keypress
    assert result == b"\r"  # Not enough content after break to be a paste


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

    # buffer = "\r" + "ab" = 1 newline, 3 chars → not paste → raw bytes
    assert result == b"\rab"
    # Plan B: raw bytes returned (not None + _pending_bytes).
    # The \r will be processed as submit by _process_input, and "ab" is
    # preserved in the same byte stream. The later "\nx" arrives in a
    # subsequent read.


# ── R4: 快速连按 Enter 时序模拟 ───────────────────────────────────────


def test_r4_double_enter_within_20ms_detected_as_paste(tmp_path, monkeypatch):
    """R4: two \\r arriving near-simultaneously → second \\r drained, first submit swallowed.

    Note: with the kbhit() fast-exit, the second char must already be in the
    buffer when kbhit() is first checked. Real pastes inject chars
    near-instantaneously, so delay=0 models that. A human double-tap (10ms+
    gap) will correctly NOT be detected as paste.
    """
    # First \r is first_char, second \r already in buffer (delay=0)
    schedule = [("\r", 0)]
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


# ── R5: 普通字符开头的多行粘贴 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_r5_regular_char_start_multiline_paste(tmp_path, monkeypatch):
    """R5: paste starting with a regular char (not \\r/\\n) must be detected.

    Windows Terminal injects pasted content character-by-character. When the
    pasted text starts with a regular character (e.g. "line1\\r\\nline2"),
    the first char 'l' is not a newline, so the old code never triggered
    drain. The leading "line1" was inserted char-by-char, and only the
    \\r in the middle triggered drain — producing a partial [Pasted text]
    token for "line2" while "line1" showed as raw text.

    Fix: _read_input_raw_win32 should probe for quickly-following chars
    after ANY first character, not just newlines.
    """
    # Simulate: "line1\r\nline2\r\nline3" injected char-by-char
    chars = list("line1\r\nline2\r\nline3")
    _install_fake_msvcrt(monkeypatch, chars)
    tui = _tui(tmp_path)

    raw = await tui._read_input_raw_win32()

    # The entire paste should be wrapped as a bracketed-paste sequence
    assert raw.startswith(b"\x1b[200~")
    assert raw.endswith(b"\x1b[201~")
    content = raw[len(b"\x1b[200~"):-len(b"\x1b[201~")]
    assert content == "line1\r\nline2\r\nline3".encode("utf-8")


@pytest.mark.asyncio
async def test_r5_regular_char_start_single_line_not_paste(tmp_path, monkeypatch):
    """R5: a single regular char with no follow-up must NOT be treated as paste.

    Guards against false positives: typing one character should return it
    as-is, not wrap it in bracketed-paste markers.
    """
    # First char 'a', no follow-up → kbhit() False → not paste
    _install_fake_msvcrt(monkeypatch, ["a"])
    tui = _tui(tmp_path)

    raw = await tui._read_input_raw_win32()

    assert raw == b"a"
    assert tui._pending_bytes == b""


@pytest.mark.asyncio
async def test_r5_regular_char_short_followup_not_paste(tmp_path, monkeypatch):
    """R5: a regular char followed by only 1-2 chars is normal typing, not paste.

    The drain heuristic (>=2 newlines or >8 chars) must still apply when
    the first char is not a newline. Plan B: all drained chars are
    returned together as raw bytes (no _pending_bytes stashing).
    """
    # 'a' + 'b' = 2 chars, 0 newlines → not a paste
    _install_fake_msvcrt(monkeypatch, ["a", "b"])
    tui = _tui(tmp_path)

    raw = await tui._read_input_raw_win32()

    # Plan B: both chars returned together, in order
    assert raw == b"ab"
    assert tui._pending_bytes == b""


@pytest.mark.asyncio
async def test_r5_e2e_regular_char_paste_collapses_to_token(tmp_path, monkeypatch):
    """R5 E2E: regular-char-start paste → single [Pasted text] token, no partial.

    This is the user-reported bug: "line1" showed as raw text while
    "line2\\nline3" showed as [Pasted text #N]. After the fix, the entire
    paste should collapse to one token.
    """
    chars = list("line1\r\nline2\r\nline3")
    _install_fake_msvcrt(monkeypatch, chars)
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    raw = await tui._read_input_raw_win32()
    assert raw.startswith(b"\x1b[200~")

    tui._process_input(raw)

    # Entire paste → single token, no partial raw text
    text = tui._get_input_text()
    assert text == "[Pasted text #1 +2 lines]"
    assert tui._paste_entries[0]["expanded"] == "line1\nline2\nline3"
    assert tui._queue.empty()


# ── R6: 分批粘贴合并为单个 token ──────────────────────────────────────


@pytest.mark.asyncio
async def test_r6_split_batch_paste_merges_to_single_token(tmp_path, monkeypatch):
    """R6: a paste split across two drain batches must produce ONE token.

    Windows Terminal may inject a large paste in batches. The 20ms drain
    timeout can fire between batches, producing two bracketed-paste
    sequences. Without merging, the user sees two adjacent tokens:
    [Pasted text #1 +4 lines][Pasted text #2 29 chars]

    Fix: when _insert_pasted_text is called and the previous paste was
    near-instantaneous, append to the existing entry instead of creating
    a new one.
    """
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    # Batch 1: "line1\r\nline2\r\nline3\r\n"
    batch1 = b"\x1b[200~line1\r\nline2\r\nline3\r\n\x1b[201~"
    # Batch 2: "line4\r\nline5"  (arrives immediately after)
    batch2 = b"\x1b[200~line4\r\nline5\x1b[201~"

    tui._process_input(batch1)
    tui._process_input(batch2)

    # Should be a SINGLE token, not two
    text = tui._get_input_text()
    assert text == "[Pasted text #1 +4 lines]", f"got: {text!r}"
    # Expanded content should be the full 5-line paste
    assert tui._paste_entries[0]["expanded"] == "line1\nline2\nline3\nline4\nline5"
    assert len(tui._paste_entries) == 1
    assert tui._queue.empty()


@pytest.mark.asyncio
async def test_r6_separate_pastes_create_separate_tokens(tmp_path, monkeypatch):
    """R6: two genuinely separate pastes (with a time gap) must NOT merge.

    Guards against over-merging: if the user pastes, types something, then
    pastes again, those should be separate tokens.
    """
    import time

    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    # First paste
    batch1 = b"\x1b[200~hello\x1b[201~"
    tui._process_input(batch1)

    # Simulate time gap (user types something between pastes)
    # Force the last-paste timestamp to be old
    tui._last_paste_time = 0.0  # type: ignore[attr-defined]

    # Second paste
    batch2 = b"\x1b[200~world\x1b[201~"
    tui._process_input(batch2)

    # Two separate tokens
    text = tui._get_input_text()
    assert "[Pasted text #1" in text
    assert "[Pasted text #2" in text
    assert len(tui._paste_entries) == 2
    # Two separate tokens
    text = tui._get_input_text()
    assert "[Pasted text #1" in text
    assert "[Pasted text #2" in text
    assert len(tui._paste_entries) == 2


# ── R7: IME 快速输入字符乱序 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_r7_fast_typing_preserves_char_order(tmp_path, monkeypatch):
    """R7: fast IME/keyboard input must NOT be reordered by the drain heuristic.

    When a user types quickly (e.g. IME commit of "hello"), multiple
    characters arrive in the console input buffer near-simultaneously.
    The old drain logic reads them all, decides they're "not a paste"
    (<2 newlines, <=8 chars), and stashes the extra chars into
    _pending_bytes. On the next read, _process_input prepends
    _pending_bytes to the new data — but the new data may be a char
    that arrived *before* the stashed chars were consumed, producing
    reordered output.

    Fix (Plan B): _read_input_raw_win32 should return ALL drained chars
    in arrival order when they don't qualify as a paste, instead of
    splitting them across _pending_bytes.
    """
    # Simulate "hello" arriving near-instantly (all in buffer at once)
    _install_fake_msvcrt(monkeypatch, list("hello"))
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    raw = await tui._read_input_raw_win32()

    # All 5 chars should be returned together, in order — NOT split
    # into b"h" + _pending_bytes=b"ello"
    assert raw == b"hello", f"expected b'hello', got {raw!r}"
    assert tui._pending_bytes == b"", f"pending_bytes should be empty, got {tui._pending_bytes!r}"


@pytest.mark.asyncio
async def test_r7_fast_typing_e2e_no_reorder(tmp_path, monkeypatch):
    """R7 E2E: typing "hello" fast then "w" must produce "hellow", not "hellow" reordered.

    This reproduces the user-reported bug: IME characters appear out of
    order in the input box. The old code returned b"h" from the first
    read (with "ello" stashed in _pending_bytes), then on the next read
    returned b"w" — but _process_input prepended _pending_bytes,
    producing b"ellow". The user saw "h" then "ellow" = "hellow" which
    *looks* correct, but if the timing differs (e.g. drain only captures
    "el" before timeout, "lo" arrives later), the order breaks.

    With Plan B, the first read returns all available chars at once,
    so there's no _pending_bytes reordering.
    """
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    # First read: "hello" all in buffer
    _install_fake_msvcrt(monkeypatch, list("hello"))
    raw1 = await tui._read_input_raw_win32()
    tui._process_input(raw1)

    # Second read: "w" (user continues typing)
    _install_fake_msvcrt(monkeypatch, ["w"])
    raw2 = await tui._read_input_raw_win32()
    tui._process_input(raw2)

    assert tui._get_input_text() == "hellow", f"got: {tui._get_input_text()!r}"


@pytest.mark.asyncio
async def test_r7_short_followup_returns_all_chars_no_pending(tmp_path, monkeypatch):
    """R7: 2-3 chars arriving together must all be returned, not stashed.

    The old code returned only the first char and stashed the rest in
    _pending_bytes. Plan B returns them all together so _process_input
    sees them in the correct order.
    """
    # 'a' + 'b' + 'c' = 3 chars, 0 newlines → not a paste, but all 3
    # should be returned together
    _install_fake_msvcrt(monkeypatch, ["a", "b", "c"])
    tui = _tui(tmp_path)

    raw = await tui._read_input_raw_win32()

    assert raw == b"abc", f"expected b'abc', got {raw!r}"
    assert tui._pending_bytes == b""


@pytest.mark.asyncio
async def test_r7_mixed_fast_input_and_paste_detection(tmp_path, monkeypatch):
    """R7: fast typing (short, no newlines) returns raw; real paste (long/multiline) still wrapped.

    Ensures Plan B doesn't break paste detection: short fast input is
    returned as-is, but a genuine paste (>=2 newlines or >8 chars) is
    still wrapped in bracketed-paste markers.
    """
    tui = _tui(tmp_path)

    # Case 1: 5 chars, no newlines → raw bytes, not paste
    _install_fake_msvcrt(monkeypatch, list("hello"))
    raw1 = await tui._read_input_raw_win32()
    assert not raw1.startswith(b"\x1b[200~"), "short input should not be paste"
    assert raw1 == b"hello"

    # Case 2: 10 chars, no newlines → paste (>8 chars)
    _install_fake_msvcrt(monkeypatch, list("abcdefghij"))
    raw2 = await tui._read_input_raw_win32()
    assert raw2.startswith(b"\x1b[200~"), "long input should be paste"
    assert raw2.endswith(b"\x1b[201~")

    # Case 3: 2+ newlines → paste
    _install_fake_msvcrt(monkeypatch, list("\r\nx\r\ny"))
    raw3 = await tui._read_input_raw_win32()
    assert raw3.startswith(b"\x1b[200~"), "multiline input should be paste"