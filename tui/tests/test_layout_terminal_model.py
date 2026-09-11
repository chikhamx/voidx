"""Cell-level acceptance tests for terminal layout, not a general VT emulator."""

import asyncio
import os
import re
import shutil
import unicodedata
from contextlib import asynccontextmanager

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from tui_helpers import _tui, setup_dock  # noqa: F401
from voidx.presentation.output.dock import dock
from voidx_cli.terminal_writer import TerminalWriter


_BEGIN_SYNC = "\x1b[?2026h"
_END_SYNC = "\x1b[?2026l"


class _VTScreen:
    """Strict CUP/EL/ED/SGR/2026 model; None marks a wide glyph's second cell.

    LF is vertical only. The stream adapter, not the parser, models ONLCR.
    CUP outside the screen is rejected rather than clamped to expose bad output.
    Styles are validated but not painted; complex emoji clusters are unsupported.
    """

    def __init__(self, *, width: int, height: int):
        self.width = width
        self.height = height
        self._cells = [[" "] * width for _ in range(height)]
        self._row = self._col = 0
        self._wrap_pending = False
        self._pending = ""
        self.scrollback = []
        self.synchronized = False
        self.sync_events = []
        self._displayed_cells = self.cells
        self._top_margin = 0
        self._bottom_margin = height - 1

    @property
    def cells(self):
        return tuple(tuple(row) for row in self._cells)

    @staticmethod
    def _row_text(row):
        return "".join(cell for cell in row if cell is not None).rstrip()

    @property
    def rows(self):
        return tuple(self._row_text(row) for row in self._cells)

    @property
    def history(self):
        return tuple(self._row_text(row) for row in self.scrollback)

    @property
    def cursor(self):
        return self._row + 1, self._col + 1

    @property
    def displayed_cells(self):
        return self._displayed_cells if self.synchronized else self.cells

    def feed(self, text: str):
        for char in text:
            if self._pending:
                if self._pending == "\x1b" and char != "[":
                    raise ValueError(f"unsupported escape: {self._pending + char!r}")
                self._pending += char
                if len(self._pending) > 2 and "@" <= char <= "~":
                    sequence, self._pending = self._pending, ""
                    self._csi(sequence)
                elif len(self._pending) > 2 and char not in "0123456789;?":
                    raise ValueError(f"unsupported CSI: {self._pending!r}")
            elif char == "\x1b":
                self._pending = char
            elif char == "\r":
                self._col = 0
                self._wrap_pending = False
            elif char == "\n":
                self._linefeed()
            else:
                self._put(char)

    def finish(self):
        if self._pending or self.synchronized:
            raise ValueError("incomplete ANSI or unclosed synchronized output")

    def _csi(self, sequence: str):
        if sequence in {_BEGIN_SYNC, _END_SYNC}:
            if sequence == _BEGIN_SYNC:
                self._displayed_cells = self.displayed_cells
                self.synchronized = True
            else:
                self.synchronized = False
            self.sync_events.append(self.synchronized)
            return
        if not re.fullmatch(r"\x1b\[[0-9;]*[HJKmrS]", sequence):
            raise ValueError(f"unsupported CSI: {sequence!r}")
        params = [int(value or "0") for value in sequence[2:-1].split(";")]
        final = sequence[-1]
        if final == "S":
            top = getattr(self, "_top_margin", 0)
            bottom = getattr(self, "_bottom_margin", self.height - 1)
            for _ in range(min(params[0] or 1, bottom - top + 1)):
                removed = self._cells.pop(top)
                if top == 0:
                    self.scrollback.append(tuple(removed))
                self._cells.insert(bottom, [" "] * self.width)
            self._wrap_pending = False
            return
        if final == "r":
            raw_params = [int(value) for value in sequence[2:-1].split(";") if value]
            if len(raw_params) == 2:
                self._top_margin = max(0, raw_params[0] - 1)
                self._bottom_margin = min(self.height - 1, raw_params[1] - 1)
            else:
                self._top_margin = 0
                self._bottom_margin = self.height - 1
            self._row, self._col = self._top_margin, 0
            return
        if final == "m":
            self._validate_sgr(params)
            return
        self._wrap_pending = False
        if final == "H":
            if len(params) > 2:
                raise ValueError(f"unsupported CUP: {sequence!r}")
            row, col = (params + [1])[:2]
            row, col = row or 1, col or 1
            if not (1 <= row <= self.height and 1 <= col <= self.width):
                raise ValueError(f"CUP out of bounds: {(row, col)}")
            self._row, self._col = row - 1, col - 1
            return
        if len(params) != 1 or params[0] not in {0, 1, 2}:
            raise ValueError(f"unsupported erase: {sequence!r}")
        mode = params[0]
        if final == "K":
            first = 0 if mode in {1, 2} else self._col
            last = self.width if mode in {0, 2} else self._col + 1
            self._erase(self._row, first, last)
        else:
            cursor = self._row * self.width + self._col
            first = 0 if mode in {1, 2} else cursor
            last = self.height * self.width if mode in {0, 2} else cursor + 1
            for index in range(first, last):
                self._erase(index // self.width, index % self.width, index % self.width + 1)

    @staticmethod
    def _validate_sgr(params):
        simple = {0, 1, 2, 3, 4, 5, 7, 8, 9, 22, 23, 24, 25, 27, 28, 29, 39, 49}
        simple.update(range(30, 38))
        simple.update(range(40, 48))
        simple.update(range(90, 98))
        simple.update(range(100, 108))
        index = 0
        while index < len(params):
            code = params[index]
            index += 1
            if code in simple:
                continue
            if code in {38, 48} and index < len(params):
                count = {2: 3, 5: 1}.get(params[index], 0)
                colors = params[index + 1:index + 1 + count]
                if count and len(colors) == count and all(0 <= c <= 255 for c in colors):
                    index += count + 1
                    continue
            raise ValueError(f"unsupported SGR: {params!r}")

    def _linefeed(self):
        self._wrap_pending = False
        top_margin = getattr(self, "_top_margin", 0)
        bottom_margin = getattr(self, "_bottom_margin", self.height - 1)
        if self._row == bottom_margin:
            if top_margin == 0:
                self.scrollback.append(tuple(self._cells.pop(0)))
            else:
                self._cells.pop(top_margin)
            self._cells.insert(bottom_margin, [" "] * self.width)
        else:
            self._row = min(self.height - 1, self._row + 1)

    def _erase(self, row: int, first: int, last: int):
        cells = self._cells[row]
        for col in range(first, last):
            if cells[col] is None:
                cells[col - 1] = " "
            elif col + 1 < self.width and cells[col + 1] is None:
                cells[col + 1] = " "
            cells[col] = " "

    def _put(self, char: str):
        if char == "\ufe0f" or 0x1F3FB <= ord(char) <= 0x1F3FF:
            raise ValueError(f"unsupported grapheme: {char!r}")
        if unicodedata.combining(char):
            col = self._col if self._wrap_pending else self._col - 1
            if col >= 0 and self._cells[self._row][col] is None:
                col -= 1
            if col < 0 or self._cells[self._row][col] == " ":
                raise ValueError("combining mark without a preceding glyph")
            self._cells[self._row][col] += char
            return
        if unicodedata.category(char)[0] in {"C", "M"}:
            raise ValueError(f"unsupported character: {char!r}")
        width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if width > self.width:
            raise ValueError("glyph wider than screen")
        if self._wrap_pending or self._col + width > self.width:
            self._col = 0
            self._linefeed()
        self._erase(self._row, self._col, self._col + width)
        self._cells[self._row][self._col] = char
        if width == 2:
            self._cells[self._row][self._col + 1] = None
        self._col += width
        if self._col == self.width:
            self._col -= 1
            self._wrap_pending = True


class _ModelStream:
    """TextIO sink with the usual POSIX OPOST/ONLCR output translation."""

    def __init__(self, screen):
        self.screen = screen
        self._writes = []

    @property
    def text(self):
        return "".join(self._writes)

    def write(self, value):
        self._writes.append(value)
        self.screen.feed(value.replace("\n", "\r\n"))
        return len(value)

    def flush(self):
        pass


def test_vt_cr_and_lf_have_distinct_cursor_semantics():
    screen = _VTScreen(width=6, height=3)
    screen.feed("ab\nX\rY")

    assert screen.rows == ("ab", "Y X", "")
    assert screen.cursor == (2, 2)
    assert screen.scrollback == []


def test_vt_lf_scrolls_only_at_the_bottom_and_preserves_history():
    screen = _VTScreen(width=5, height=2)
    screen.feed("one\r\ntwo")
    assert screen.scrollback == []

    screen.feed("\r\nthree")
    assert screen.rows == ("two", "three")
    assert screen.history == ("one",)
    screen.feed("\x1b[2J")
    assert screen.rows == ("", "")
    assert screen.history == ("one",)
    assert screen.cursor == (2, 5)


@pytest.mark.parametrize("home", ["\x1b[H", "\x1b[;H", "\x1b[0;0H"])
def test_vt_cup_defaults_to_home_and_overwrites_without_scrolling(home):
    screen = _VTScreen(width=6, height=3)
    screen.feed("\x1b[3;4Hx" + home + "y")

    assert screen.rows == ("y", "", "   x")
    assert screen.cursor == (1, 2)
    assert screen.scrollback == []


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("\x1b[K", ("abcde", "fg", "klmno")),
        ("\x1b[1K", ("abcde", "   ij", "klmno")),
        ("\x1b[2K", ("abcde", "", "klmno")),
        ("\x1b[J", ("abcde", "fg", "")),
        ("\x1b[1J", ("", "   ij", "klmno")),
        ("\x1b[2J", ("", "", "")),
    ],
)
def test_vt_erase_modes_are_inclusive_and_do_not_move_cursor(sequence, expected):
    screen = _VTScreen(width=5, height=3)
    screen.feed("abcde\r\nfghij\r\nklmno\x1b[2;3H")
    screen.feed(sequence)

    assert screen.rows == expected
    assert screen.cursor == (2, 3)
    assert screen.scrollback == []


def test_vt_delays_autowrap_until_next_printable_character():
    screen = _VTScreen(width=4, height=2)
    screen.feed("abcd")
    assert screen.rows == ("abcd", "")
    assert screen.cursor == (1, 4)
    screen.feed("\x1b[31mX\x1b[0m")
    assert screen.rows == ("abcd", "X")
    assert screen.cursor == (2, 2)

    screen.feed("\x1b[2;1H1234\r\nZ")
    assert screen.history == ("abcd",)
    assert screen.rows == ("1234", "Z")


def test_vt_cup_cancels_pending_wrap_even_with_sgr_at_right_margin():
    screen = _VTScreen(width=4, height=2)
    screen.feed("abcd\x1b[0m\x1b[1;1HX")

    assert screen.rows == ("Xbcd", "")
    assert screen.scrollback == []


def test_vt_wide_characters_use_two_cells_and_wrap_before_a_split_glyph():
    screen = _VTScreen(width=4, height=3)
    screen.feed("ab界")
    assert screen.cells[0] == ("a", "b", "界", None)
    assert screen.cursor == (1, 4)

    screen.feed("😀")
    assert screen.cells[1] == ("😀", None, " ", " ")
    screen.feed("x界")
    assert screen.rows == ("ab界", "😀x", "界")
    assert screen.cursor == (3, 3)
    assert screen.scrollback == []


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ("\x1b[1;2Hx", (" ", "x", "a", " ")),
        ("\x1b[1;1Hx", ("x", " ", "a", " ")),
        ("\x1b[1;2H\x1b[K", (" ", " ", " ", " ")),
        ("\x1b[1;1H\x1b[1K", (" ", " ", "a", " ")),
    ],
)
def test_vt_overwrite_and_erase_never_leave_half_a_wide_glyph(update, expected):
    screen = _VTScreen(width=4, height=2)
    screen.feed("界a" + update)

    assert screen.cells[0] == expected
    assert screen.scrollback == []


def test_vt_combining_mark_attaches_without_consuming_a_cell():
    screen = _VTScreen(width=4, height=2)
    screen.feed("e\u0301界\u0301")

    assert screen.cells[0] == ("e\u0301", "界\u0301", None, " ")
    assert screen.cursor == (1, 4)


def test_vt_sgr_is_cell_neutral_and_csi_can_span_stream_writes():
    screen = _VTScreen(width=5, height=2)
    for char in "\x1b[1;38;2;12;34;56mA\x1b[48;5;123m界\x1b[m":
        screen.feed(char)
    screen.finish()

    assert screen.cells[0] == ("A", "界", None, " ", " ")
    assert screen.cursor == (1, 4)


def test_vt_synchronized_output_defers_presentation_until_end():
    screen = _VTScreen(width=4, height=2)
    screen.feed("old")
    before = screen.displayed_cells
    screen.feed(_BEGIN_SYNC + "\rnew\r\nrow")

    assert screen.rows == ("new", "row")
    assert screen.displayed_cells == before
    assert screen.synchronized is True
    screen.feed(_END_SYNC)
    screen.finish()
    assert screen.displayed_cells == screen.cells
    assert screen.sync_events == [True, False]


@pytest.mark.parametrize(
    "sequence",
    [
        "\x1b[1A", "\x1b[2T", "\x1b[?1049h", "\x1b[3J", "\x1b[3K",
        "\x1b[999m", "\x1b[38;2;255;0m", "\x1b[48;5;256m",
        "\x1b]8;;https://example.com\x1b\\", "\x1b7", "\t", "\b", "\x07",
        "\u200d", "\ufe0f", "\u0301", "\U0001f3fb",
    ],
)
def test_vt_unsupported_sequences_and_graphemes_fail_explicitly(sequence):
    screen = _VTScreen(width=6, height=3)
    with pytest.raises(ValueError):
        screen.feed(sequence)
        screen.finish()


@pytest.mark.parametrize("sequence", ["\x1b", "\x1b[", "\x1b[2;", _BEGIN_SYNC])
def test_vt_finish_rejects_incomplete_ansi_or_unclosed_synchronized_output(sequence):
    screen = _VTScreen(width=6, height=3)
    screen.feed(sequence)
    with pytest.raises(ValueError):
        screen.finish()


@pytest.mark.parametrize("sequence", ["\x1b[4;1H", "\x1b[1;7H"])
def test_vt_rejects_out_of_bounds_cup_instead_of_hiding_it_by_clamping(sequence):
    screen = _VTScreen(width=6, height=3)
    with pytest.raises(ValueError, match="bounds"):
        screen.feed(sequence)


def test_model_stream_explicitly_emulates_tty_onlcr_without_changing_raw_log():
    screen = _VTScreen(width=5, height=2)
    stream = _ModelStream(screen)
    assert stream.write("ab\nX") == 4
    stream.flush()

    assert screen.rows == ("ab", "X")
    assert screen.cursor == (2, 2)
    assert stream.text == "ab\nX"


@asynccontextmanager
async def _terminal_tui(tmp_path, monkeypatch, *, worker, height=12):
    monkeypatch.setattr(
        shutil, "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, height)),
    )
    screen = _VTScreen(width=80, height=height)
    stream = _ModelStream(screen)
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(force_terminal=True, width=80, height=height, _environ={})
    monkeypatch.setattr(tui, "_render_hint_lines", lambda: [Text("status")])
    writer = TerminalWriter(stream, byte_budget=23)
    tui._terminal_writer = writer
    errors = []
    if worker:
        writer.start(
            loop=asyncio.get_running_loop(),
            on_frame_result=tui._handle_terminal_frame_result,
            on_error=errors.append,
        )
        assert writer.worker_alive

    async def drain():
        if worker:
            await asyncio.wait_for(writer.drain_async(), timeout=5)
            tasks = tuple(tui._render_state.pending_commit_tasks.values())
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        else:
            writer.flush()
        assert not errors
        screen.finish()

    try:
        yield tui, screen, stream, drain
    finally:
        if worker:
            await asyncio.wait_for(writer.shutdown_async(), timeout=5)
        assert not errors


def _assert_applied_screen(tui, screen):
    snapshot = tui._applied_layout_snapshot
    assert snapshot is not None
    target = tuple(tui._prev_frame_lines)
    regions = tuple(
        row
        for region in snapshot.regions[:-1]
        for row in region.content_signature[-1]
    ) + snapshot.bottom.rendered.rows
    assert regions == target
    assert len(target) == snapshot.frame_rows <= screen.height
    start = snapshot.frame_start_row - 1
    end = start + len(target)
    expected = tuple(Text.from_ansi(line).plain.rstrip() for line in target)
    assert all(cell_len(line) <= screen.width for line in expected)
    assert screen.rows[start:end] == expected
    assert screen.rows[end:] == ("",) * (screen.height - end)
    assert screen.cursor == (snapshot.cursor_row, snapshot.cursor_col)
    assert snapshot.bottom.input.start_row <= snapshot.cursor_row < (
        snapshot.bottom.input.start_row + snapshot.bottom.input.visual_rows
    )
    return snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_input_patch_and_shrinking_bottom_clear_old_rows(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker) as (
        tui, screen, stream, drain
    ):
        dock.tree.new_node(parent=dock.tree.root, node_type="message", header="transcript")
        tui._input_lines = ["wide 界面", "second input", "third input"]
        tui._cursor_row, tui._cursor_col = 2, 4
        tui._render_frame()
        await drain()
        original = _assert_applied_screen(tui, screen)
        before = len(stream.text)

        tui._input_lines[0] = "new 中文"
        tui._render_input_region()
        await drain()
        _assert_applied_screen(tui, screen)
        patch = stream.text[before:]
        assert "\x1b[J" not in patch
        assert "\x1b[K" in patch
        assert patch.startswith(_BEGIN_SYNC)
        assert _END_SYNC in patch
        assert screen.scrollback == []

        before = len(stream.text)
        tui._input_lines = ["short"]
        tui._cursor_row, tui._cursor_col = 0, 3
        tui._render_input_region()
        await drain()
        current = _assert_applied_screen(tui, screen)
        assert current.frame_rows < original.frame_rows
        patch = stream.text[before:]
        assert "\x1b[J" not in patch
        for row in range(
            current.frame_start_row + current.frame_rows,
            original.frame_start_row + original.frame_rows,
        ):
            assert f"\x1b[{row};1H\x1b[K" in patch
            assert screen.rows[row - 1] == ""
        assert screen.scrollback == []




@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_thinking_shrink_does_not_clear_new_bottom_rows(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker) as (
        tui, screen, stream, drain
    ):
        dock.begin_capture()
        dock.start_turn("thinking question")
        dock.set_stream(
            "\n".join(f"thinking line {index}" for index in range(5)),
            phase="thinking",
        )
        tui._render_frame()
        await drain()
        original = _assert_applied_screen(tui, screen)
        before = len(stream.text)

        dock.set_stream("thinking line 0", phase="thinking")
        tui._render_frame()
        await drain()
        current = _assert_applied_screen(tui, screen)

        assert current.frame_rows < original.frame_rows
        patch = stream.text[before:]
        assert "\x1b[J" not in patch
        assert "\x1b[K" in patch
        assert screen.scrollback == []


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_thinking_blank_rows_are_not_left_after_shrink(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker) as (
        tui, screen, stream, drain
    ):
        dock.begin_capture()
        dock.start_turn("blank thinking question")
        dock.set_stream("visible\n  \nvisible tail", phase="thinking")
        tui._render_frame()
        await drain()
        original = _assert_applied_screen(tui, screen)
        before = len(stream.text)

        dock.set_stream("visible", phase="thinking")
        tui._render_frame()
        await drain()
        current = _assert_applied_screen(tui, screen)

        assert current.frame_rows < original.frame_rows
        assert screen.scrollback == []
        assert "\x1b[J" not in stream.text[before:]
        assert "\x1b[K" in stream.text[before:]


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_thinking_to_text_clears_thinking_rows_once(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker) as (
        tui, screen, stream, drain
    ):
        dock.begin_capture()
        dock.start_turn("text after thinking")
        dock.set_stream("thinking line 0\nthinking line 1", phase="thinking")
        tui._render_frame()
        await drain()
        original = _assert_applied_screen(tui, screen)
        before = len(stream.text)

        dock.set_stream("final answer", phase="text")
        tui._render_frame()
        await drain()
        current = _assert_applied_screen(tui, screen)

        assert current.frame_rows <= original.frame_rows
        assert "final answer" in "\n".join(screen.rows)
        assert screen.scrollback == []
        assert "\x1b[J" not in stream.text[before:]
        assert "\x1b[K" in stream.text[before:]


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_thinking_all_blank_content_uses_no_rows(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker) as (
        tui, screen, stream, drain
    ):
        dock.begin_capture()
        dock.start_turn("blank only")
        dock.set_stream("\n\n", phase="thinking")
        tui._render_frame()
        await drain()

        current = _assert_applied_screen(tui, screen)
        thinking = tui._last_render_plan.logical_plan.source_regions[2]

        assert thinking.visual_rows == 0
        assert current.frame_rows <= 12
        assert screen.scrollback == []
        assert "thinking" not in "\n".join(screen.rows).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_thinking_space_content_is_one_real_row(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker) as (
        tui, screen, stream, drain
    ):
        dock.begin_capture()
        dock.start_turn("space thinking")
        dock.set_stream("   ", phase="thinking")
        tui._render_frame()
        await drain()

        current = _assert_applied_screen(tui, screen)
        thinking = tui._last_render_plan.logical_plan.source_regions[2]

        assert thinking.visual_rows == 1
        assert current.frame_rows <= 12
        assert screen.scrollback == []
        assert "space thinking" in "\n".join(screen.rows)

@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_large_logical_transcript_commits_once_without_frame_scroll(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker) as (
        tui, screen, stream, drain
    ):
        for index in range(260):
            dock.tree.new_node(
                parent=dock.tree.root,
                node_type="message",
                header=f"RETAINED-{index:03d}",
                status="done",
            )
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)
        logical = tui._last_render_plan.logical_plan.source_regions[0]
        assert logical.visual_rows >= 260
        assert "RETAINED-000" in logical.ansi
        assert screen.scrollback == []
        retained_nodes = dock.tree.node_count

        for text in ("x", "xy", "xyz"):
            tui._input_lines = [text]
            tui._cursor_col = len(text)
            tui._render_input_region()
            await drain()
            _assert_applied_screen(tui, screen)
            assert screen.scrollback == []
            assert dock.tree.node_count == retained_nodes

        before = len(stream.text)
        tui._flush_committed(force=True)
        await drain()
        commit = stream.text[before:]
        for index in range(260):
            assert commit.count(f"RETAINED-{index:03d}") == 1
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)
        all_rows = "\n".join((*screen.history, *screen.rows))
        for index in range(260):
            assert all_rows.count(f"RETAINED-{index:03d}") == 1
        history_after_commit = screen.history

        before = len(stream.text)
        tui._flush_committed(force=True)
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)
        assert "RETAINED-" not in stream.text[before:]
        assert screen.history == history_after_commit
        assert tui._last_render_plan.logical_plan.source_regions[0].visual_rows == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_frame_growth_scrolls_only_committed_rows(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (
        tui, screen, stream, drain
    ):
        for index in range(6):
            dock.tree.new_node(
                parent=dock.tree.root, node_type="message", header=f"HISTORY-{index}"
            )
        tui._flush_committed(force=True)
        await drain()
        tui._render_frame()
        await drain()
        previous = _assert_applied_screen(tui, screen)
        assert previous.frame_start_row == 7
        assert screen.scrollback == []
        before = len(stream.text)

        tui._input_lines = [f"dynamic input {index}" for index in range(8)]
        tui._cursor_row, tui._cursor_col = 7, 5
        tui._render_frame()
        await drain()
        current = _assert_applied_screen(tui, screen)
        assert current.scroll_epoch > previous.scroll_epoch
        assert screen.history
        assert all(row.startswith("HISTORY-") for row in screen.history)
        assert len(screen.history) <= 6
        assert "\x1b[12;1H\n" in stream.text[before:]
        all_rows = "\n".join((*screen.history, *screen.rows))
        for index in range(6):
            assert all_rows.count(f"HISTORY-{index}") == 1

        history = screen.history
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)
        assert screen.history == history


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_submit_preserves_unchanged_activity_on_screen(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker) as (
        tui, screen, stream, drain
    ):
        tui._busy = True
        monkeypatch.setattr(
            tui, "_render_busy_activity_elements", lambda width: [Text("Working (5s)")]
        )
        tui._render_frame()
        await drain()
        assert "Working (5s)" in screen.rows

        assert tui._process_input(b"hello") is True
        tui._render_after_input()
        await drain()
        _assert_applied_screen(tui, screen)
        before = len(stream.text)
        assert tui._process_input(b"\r") is True
        tui._render_after_input()
        await drain()

        _assert_applied_screen(tui, screen)
        assert tui._queue.get_nowait() == "hello"
        assert "Working (5s)" in screen.rows
        assert "hello" not in "\n".join(screen.rows)
        patch = stream.text[before:]
        assert "Working" not in patch
        assert "\x1b[J" not in patch
        assert patch.startswith(_BEGIN_SYNC)
        assert patch.endswith(_END_SYNC)
        assert screen.scrollback == []



@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_live_thinking_and_busy_do_not_enter_scrollback(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (
        tui, screen, stream, drain
    ):
        for index in range(6):
            dock.tree.new_node(
                parent=dock.tree.root,
                node_type="message",
                header=f"HISTORY-{index}",
            )
        tui._flush_committed(force=True)
        await drain()

        dock.begin_capture()
        dock.start_turn("live overlay question")
        tui._busy = True
        tui._was_busy = True
        tui._busy_started_at = 0.0
        tui._busy_activity_verb = "Ruminating"
        dock.set_stream("checking permissions", phase="thinking")
        tui._flush_committed()
        await drain()
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)

        history = "\n".join(screen.history)
        visible = "\n".join(screen.rows)
        assert "Ruminating" not in history
        assert "Thinking" not in history
        assert "checking permissions" not in history
        assert "checking permissions" in visible
        assert "Thinking" in visible

        tui._input_lines = [f"dynamic input {index}" for index in range(8)]
        tui._cursor_row, tui._cursor_col = 7, 5
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)

        history = "\n".join(screen.history)
        assert "Ruminating" not in history
        assert "Thinking" not in history
        assert "checking permissions" not in history
        assert all(
            row.startswith("HISTORY-") or not row.strip()
            for row in screen.history
        )

        dock.commit_stream()
        tui._busy = False
        tui._flush_committed()
        await drain()
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)

        all_rows = "\n".join((*screen.history, *screen.rows))
        assert "Ruminating" not in all_rows
        assert "Thinking" not in all_rows
        assert "checking permissions" not in all_rows
        assert all_rows.count("live overlay question") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_search_started_and_completed_share_one_scrollback_row(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (
        tui, screen, stream, drain
    ):
        for index in range(6):
            dock.tree.new_node(
                parent=dock.tree.root,
                node_type="message",
                header=f"HISTORY-{index}",
            )
        tui._flush_committed(force=True)
        await drain()

        dock.begin_capture()
        dock.start_turn("find compaction summary")
        tui._busy = True
        tui._was_busy = True
        tui._busy_started_at = 0.0
        tui._busy_activity_verb = "Ruminating"
        tool = dock.start_tool(
            "Searching",
            'pattern="_compaction_summary"',
            tool_name="search",
            tool_call_id="search-1",
            raw_args={"pattern": "_compaction_summary"},
        )
        tui._flush_committed()
        await drain()
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)

        started = "\n".join((*screen.history, *screen.rows))
        assert started.count('Search("_compaction_summary")') == 1
        assert "0 matches" not in started
        assert "Ruminating" not in "\n".join(screen.history)
        assert "Search" not in "\n".join(screen.history)

        tui._busy = False
        tui._flush_committed()
        await drain()
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)

        mid = "\n".join((*screen.history, *screen.rows))
        assert mid.count('Search("_compaction_summary")') == 1
        assert "0 matches" not in mid
        assert "Search" not in "\n".join(screen.history)

        dock.finish_tool_node(tool, "Search", 0.1, True, "0 matches")
        tui._flush_committed()
        await drain()
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)

        all_rows = "\n".join((*screen.history, *screen.rows))
        assert all_rows.count('Search("_compaction_summary")') == 1
        assert all_rows.count("0 matches") == 1
        assert "Ruminating" not in all_rows
        assert all_rows.count("find compaction summary") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_commit_without_frame_result_never_leaks_status_or_busy(
    tmp_path, monkeypatch, worker
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (
        tui, screen, stream, drain
    ):
        for index in range(4):
            dock.append_message(f"HISTORY-{index}")
        tui._flush_committed(force=True)
        await drain()

        dock.begin_capture()
        dock.start_turn("test question")
        tui._busy = True
        tui._was_busy = True
        tui._busy_started_at = 0.0
        tui._busy_activity_verb = "Kneading"
        tui._render_frame()

        tool = dock.start_tool(
            "Searching",
            'pattern="find_something"',
            tool_name="search",
            tool_call_id="search-1",
            raw_args={"pattern": "find_something"},
        )
        dock.finish_tool_node(tool, "Search", 0.1, True, "done")
        tui._flush_committed()
        await drain()
        tui._render_frame()
        await drain()

        # Commit additional tools that fill and exceed terminal height, forcing scrollback
        tool2 = dock.start_tool(
            "Updating",
            'file_path="src/app.py"',
            tool_name="update",
            tool_call_id="update-1",
            raw_args={"file_path": "src/app.py"},
        )
        dock.finish_tool_node(tool2, "Update", 0.1, True, "done")
        tui._flush_committed()
        await drain()

        tool3 = dock.start_tool(
            "Reading",
            'file_path="src/app.py"',
            tool_name="read",
            tool_call_id="read-1",
            raw_args={"file_path": "src/app.py"},
        )
        dock.finish_tool_node(tool3, "Read", 0.1, True, "done")
        tui._flush_committed()
        await drain()

        # Check history and current rows: live overlays must never enter history
        history_text = "\n".join(screen.history)
        assert "Kneading" not in history_text
        assert "status" not in history_text
        # And in current visible rows, history must be clean
        search_rows = [i for i, r in enumerate(screen.rows) if "Search" in r]
        update_rows = [i for i, r in enumerate(screen.rows) if "Updating" in r or "Update" in r]
        read_rows = [i for i, r in enumerate(screen.rows) if "Read" in r]
        assert search_rows, f"Search missing from visible rows: {screen.rows}"
        assert update_rows, f"Update missing from visible rows: {screen.rows}"
        assert read_rows, f"Read missing from visible rows: {screen.rows}"
        assert search_rows[0] < update_rows[0] < read_rows[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
@pytest.mark.parametrize("input_count", [6, 9], ids=["safe-region", "no-region"])
async def test_terminal_model_commit_then_frame_growth_keeps_live_overlay_out_of_scrollback(
    tmp_path, monkeypatch, worker, input_count
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (
        tui, screen, stream, drain
    ):
        for index in range(6):
            dock.tree.new_node(
                parent=dock.tree.root,
                node_type="message",
                header=f"HISTORY-{index}",
            )
        tui._flush_committed(force=True)
        await drain()

        dock.begin_capture()
        dock.start_turn("commit-growth question")
        tui._busy = True
        tui._was_busy = True
        tui._busy_started_at = 0.0
        tui._busy_activity_verb = "Ruminating"
        monkeypatch.setattr(
            tui,
            "_render_busy_activity_elements",
            lambda width: [Text("LIVE-ACTIVITY")],
        )
        dock.set_stream("LIVE-THINKING-0\nLIVE-THINKING-1", phase="thinking")
        tui._input_lines = ["LIVE-INPUT-0", "LIVE-INPUT-1"]
        tui._cursor_row, tui._cursor_col = 1, 4
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)
        assert "LIVE-THINKING-0" in "\n".join(screen.rows)
        assert "LIVE-ACTIVITY" in "\n".join(screen.rows)

        dock.commit_stream()
        tui._busy = False
        tui._flush_committed()
        await drain()
        tui._render_frame()
        await drain()

        before_growth = len(stream.text)
        tui._input_lines = [f"UNCOMMITTED-INPUT-{index}" for index in range(input_count)]
        tui._cursor_row, tui._cursor_col = input_count - 1, 5
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)

        history = "\n".join(screen.history)
        assert "LIVE-ACTIVITY" not in history
        assert "LIVE-THINKING-0" not in history
        assert "LIVE-THINKING-1" not in history
        assert "UNCOMMITTED-INPUT" not in history
        assert "HISTORY-" in history
        visible = "\n".join(screen.rows)
        assert "UNCOMMITTED-INPUT" in visible

        growth_output = stream.text[before_growth:]
        if input_count == 6:
            assert "\x1b[1;3r" in growth_output
            assert "\x1b[r" in growth_output
        else:
            assert not re.search(r"\x1b\[[0-9;]*r", growth_output)
        assert "\x1b[12;1H\n" not in growth_output


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_terminal_model_multiline_commit_preserves_exact_baseline(tmp_path, monkeypatch, worker):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (
        tui, screen, stream, drain
    ):
        tui._render_frame()
        await drain()
        for batch in range(3):
            for index in range(3):
                dock.tree.new_node(parent=dock.tree.root, node_type="message",
                                   header=f"COMMIT-{batch}-{index}")
            tui._flush_committed(force=True)
            await drain()
            if worker and tui._terminal_writer._baseline_valid:
                start = tui._terminal_writer._applied_start_row - 1
                expected = tuple(Text.from_ansi(line).plain.rstrip()
                                 for line in tui._terminal_writer._applied_lines)
                assert screen.rows[start:start + len(expected)] == expected
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)
        text = "\n".join((*screen.history, *screen.rows))
        for batch in range(3):
            for index in range(3):
                assert text.count(f"COMMIT-{batch}-{index}") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
@pytest.mark.parametrize("count", [12, 29])
async def test_terminal_model_commit_at_viewport_edge_has_no_trailing_scroll(
    tmp_path, monkeypatch, worker, count
):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (
        tui, screen, stream, drain
    ):
        # Establish the actual pinned dock before exercising continuous commits.
        tui._visible_committed_rows = 12
        tui._render_frame()
        await drain()
        bottom_start = tui._last_bottom_start_row
        bottom = screen.rows[bottom_start - 1:]
        for batch in range(2):
            for index in range(count):
                dock.tree.new_node(parent=dock.tree.root, node_type="message",
                                   header=f"EDGE-{batch}-{index:02d}")
            before = len(stream.text)
            tui._flush_committed(force=True)
            await drain()
            assert screen.rows[bottom_start - 2] == f"EDGE-{batch}-{count - 1:02d}"
            assert screen.rows[bottom_start - 1:] == bottom
            assert "\n" not in stream.text[before:]
            assert f"\x1b[1;{bottom_start - 1}r" in stream.text[before:]
            assert "\x1b[r" in stream.text[before:]
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)
        text = "\n".join((*screen.history, *screen.rows))
        for batch in range(2):
            for index in range(count):
                assert text.count(f"EDGE-{batch}-{index:02d}") == 1
        assert "status" not in "\n".join(screen.history)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_input_cursor_target_matches_projected_physical_cursor(tmp_path, monkeypatch, worker):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=6) as (
        tui, screen, stream, drain
    ):
        tui._input_lines = [f"input {index}" for index in range(15)]
        tui._cursor_row, tui._cursor_col = 13, 3
        tui._render_frame()
        await drain()
        snapshot = tui._applied_layout_snapshot
        sequence, _ = tui._input_cursor_target()
        assert sequence == f"\x1b[{snapshot.cursor_row};{snapshot.cursor_col}H"


def test_vt_scroll_up_is_bounded_and_does_not_move_cursor():
    screen = _VTScreen(width=8, height=4)
    screen.feed("one\r\ntwo\r\nthree\r\nbottom")
    screen.feed("\x1b[1;3r\x1b[2;2H\x1b[1S\x1b[r")
    assert screen.rows == ("two", "three", "", "bottom")
    assert screen.history == ("one",)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_continuous_overflow_commit_then_frame_does_not_repaint_bottom(tmp_path, monkeypatch, worker):
    async with _terminal_tui(tmp_path, monkeypatch, worker=worker, height=12) as (tui, screen, stream, drain):
        tui._visible_committed_rows = 12
        tui._render_frame()
        await drain()
        bottom_start = tui._last_bottom_start_row
        bottom = screen.rows[bottom_start - 1:]
        for batch in range(2):
            for index in range(15):
                dock.tree.new_node(parent=dock.tree.root, node_type="message", header=f"BATCH-{batch}-{index:02d}")
            before = len(stream.text)
            tui._flush_committed(force=True)
            await drain()
            assert "\n" not in stream.text[before:]
            assert screen.rows[bottom_start - 1:] == bottom
            before = len(stream.text)
            tui._render_frame()
            await drain()
            _assert_applied_screen(tui, screen)
            assert "status" not in stream.text[before:]
            assert "\x1b[J" not in stream.text[before:]
        all_rows = "\n".join((*screen.history, *screen.rows))
        for batch in range(2):
            for index in range(15):
                assert all_rows.count(f"BATCH-{batch}-{index:02d}") == 1
