from tui_helpers import *  # noqa: F403

import asyncio
import re

import pytest
from rich.console import Console

from voidx.ui.tools.clipboard_image import ClipboardImageResult
from voidx.ui.tools.clipboard_text import ClipboardTextResult

def test_paste_clipboard_image_inserts_image_token(tmp_path, monkeypatch):
    def fake_paste(_workspace: str) -> ClipboardImageResult:
        return ClipboardImageResult(
            status="ok",
            message="Pasted image",
            rel_path=".voidx/attachments/clip.png",
            size=123,
        )

    monkeypatch.setattr("voidx.ui.tui.app.paste_clipboard_image_from_system", fake_paste)
    tui = _tui(tmp_path)

    result = tui.paste_clipboard_image()

    assert result.ok
    assert tui._get_input_text() == "[Pasted image #1 123B] "
    assert tui._notice == "Pasted image"


def test_paste_clipboard_image_submit_expands_to_image_token(tmp_path, monkeypatch):
    def fake_paste(_workspace: str) -> ClipboardImageResult:
        return ClipboardImageResult(
            status="ok",
            message="Pasted image",
            rel_path=".voidx/attachments/clip.png",
            size=2048,
        )

    monkeypatch.setattr("voidx.ui.tui.app.paste_clipboard_image_from_system", fake_paste)
    tui = _tui(tmp_path)

    tui.paste_clipboard_image()
    tui._process_input(b"describe")
    tui._process_input(b"\r")

    assert tui._queue.get_nowait() == "[image-clip] describe"


def test_ctrl_v_pastes_clipboard_image_when_available(tmp_path, monkeypatch):
    def fake_paste_image(_workspace: str) -> ClipboardImageResult:
        return ClipboardImageResult(
            status="ok",
            message="Pasted image",
            rel_path=".voidx/attachments/clip.png",
            size=123,
        )

    def fail_text_paste() -> ClipboardTextResult:
        raise AssertionError("text fallback should not run when image paste succeeds")

    monkeypatch.setattr("voidx.ui.tui.app.paste_clipboard_image_from_system", fake_paste_image)
    monkeypatch.setattr("voidx.ui.tui.app.paste_clipboard_text_from_system", fail_text_paste)
    tui = _tui(tmp_path)

    tui._process_input(b"\x16")

    assert tui._get_input_text() == "[Pasted image #1 123B] "
    assert tui._paste_entries[0]["expanded"] == "[image-clip]"


def test_ctrl_v_falls_back_to_clipboard_text_when_no_image(tmp_path, monkeypatch):
    def fake_paste_image(_workspace: str) -> ClipboardImageResult:
        return ClipboardImageResult(status="no_image", message="Clipboard does not contain an image.")

    def fake_paste_text() -> ClipboardTextResult:
        return ClipboardTextResult(status="ok", message="Pasted text", text="hello\nworld")

    monkeypatch.setattr("voidx.ui.tui.app.paste_clipboard_image_from_system", fake_paste_image)
    monkeypatch.setattr("voidx.ui.tui.app.paste_clipboard_text_from_system", fake_paste_text)
    tui = _tui(tmp_path)

    tui._process_input(b"\x16")

    assert tui._get_input_text() == "[Pasted text #1 +1 lines]"
    assert tui._paste_entries[0]["expanded"] == "hello\nworld"
    assert tui._queue.empty()


def test_bracketed_paste_multiline_text_collapses_to_token(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    paste_data = b"\x1b[200~line1\r\nline2\r\nline3\x1b[201~"
    tui._process_input(paste_data)

    assert tui._get_input_text() == "[Pasted text #1 +2 lines]"
    assert tui._paste_entries[0]["expanded"] == "line1\nline2\nline3"
    assert tui._queue.empty()


def test_bracketed_paste_large_text_collapses_to_token(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    pasted = "line1\nline2\nline3\nline4"

    tui._process_input(b"\x1b[200~" + pasted.encode() + b"\x1b[201~")

    assert tui._get_input_text() == "[Pasted text #1 +3 lines]"
    assert tui._paste_entries[0]["expanded"] == pasted
    assert tui._queue.empty()


def test_empty_bracketed_paste_falls_back_to_clipboard_image(tmp_path, monkeypatch):
    def fake_paste(_workspace: str) -> ClipboardImageResult:
        return ClipboardImageResult(
            status="ok",
            message="Pasted image",
            rel_path=".voidx/attachments/clip.png",
            size=123,
        )

    monkeypatch.setattr("voidx.ui.tui.app.paste_clipboard_image_from_system", fake_paste)
    tui = _tui(tmp_path)
    tui._tty = True

    tui._process_input(b"\x1b[200~\x1b[201~")

    assert tui._get_input_text() == "[Pasted image #1 123B] "
    assert tui._paste_entries[0]["expanded"] == "[image-clip]"
    assert tui._notice == "Pasted image"
    assert tui._queue.empty()


def test_collapsed_paste_submit_expands_to_full_text(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    pasted = "line1\nline2\nline3\nline4"
    token = "[Pasted text #1 +3 lines]"

    tui._process_input(b"\x1b[200~" + pasted.encode() + b"\x1b[201~")
    tui._process_input(b"\r")

    assert tui._queue.get_nowait() == f"<pasted>\n{pasted}\n</pasted>"
    assert tui._input_history == [token]
    assert tui._get_input_text() == ""


def test_collapsed_paste_history_restores_registry(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    pasted = "line1\nline2\nline3\nline4"
    token = "[Pasted text #1 +3 lines]"

    tui._process_input(b"\x1b[200~" + pasted.encode() + b"\x1b[201~")
    tui._process_input(b"\r")
    tui._process_input(b"\x1b[A")
    tui._process_input(b"\r")

    expected = f"<pasted>\n{pasted}\n</pasted>"
    assert tui._queue.get_nowait() == expected
    assert tui._queue.get_nowait() == expected
    assert tui._input_history == [token]


@pytest.mark.asyncio
async def test_interrupted_submit_restores_collapsed_paste_token(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    pasted = "line1\nline2\nline3\nline4"
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def on_submit(text: str) -> bool:
        assert text == f"<pasted>\n{pasted}\n</pasted>"
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    tui._process_input(b"\x1b[200~" + pasted.encode() + b"\x1b[201~")
    tui._process_input(b"\r")

    consumer = asyncio.create_task(tui._consume(on_submit))
    await started.wait()

    tui._handle_interrupt()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)
    consumer.cancel()

    assert tui._get_input_text() == "[Pasted text #1 +3 lines]"
    assert tui._paste_entries[0]["expanded"] == pasted
    assert tui._notice == "Interrupted. Restored last message for editing."


def test_user_typed_paste_lookalike_does_not_expand(tmp_path):
    tui = _tui(tmp_path)
    token = "[Pasted text #1 +3 lines]"
    tui._input_lines = [token]
    tui._cursor_col = len(token)

    tui._process_input(b"\r")

    assert tui._queue.get_nowait() == token


def test_registered_paste_tokens_render_dim_cyan(tmp_path):
    tui = _tui(tmp_path)
    display = tui._register_text_paste("line1\nline2\nline3\nline4")
    tui._input_lines = [display]
    tui._cursor_row = 0
    tui._cursor_col = 0
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    ansi = tui._capture_renderable(tui._render_bottom_impl(), tui._frame_width())
    plain = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", ansi)

    assert display in plain
    assert "\x1b[2;36m" in ansi or "\x1b[36;2m" in ansi or "\x1b[2m" in ansi


def test_bracketed_paste_single_line_does_not_submit(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    paste_data = b"\x1b[200~hello world\x1b[201~"
    tui._process_input(paste_data)

    assert tui._get_input_text() == "[Pasted text #1 11 chars]"
    assert tui._paste_entries[0]["expanded"] == "hello world"
    assert tui._queue.empty()


def test_bracketed_paste_with_trailing_key(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    # Paste followed by a regular keypress
    paste_data = b"\x1b[200~text\x1b[201~x"
    tui._process_input(paste_data)

    assert tui._get_input_text() == "[Pasted text #1 4 chars]x"
    assert tui._paste_entries[0]["expanded"] == "text"
    assert tui._queue.empty()


def test_bracketed_paste_split_across_reads(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    # First read: paste start + partial content
    tui._process_input(b"\x1b[200~line1\r\n")
    assert tui._paste_buffer is not None
    assert tui._queue.empty()

    # Second read: rest of content + paste end
    tui._process_input(b"line2\x1b[201~")
    assert tui._paste_buffer is None
    assert tui._get_input_text() == "[Pasted text #1 +1 lines]"
    assert tui._paste_entries[0]["expanded"] == "line1\nline2"
    assert tui._queue.empty()


def test_bracketed_paste_cr_only_normalised_to_newline(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    # Bare CR (no LF) should also become a newline
    paste_data = b"\x1b[200~line1\rline2\x1b[201~"
    tui._process_input(paste_data)

    assert tui._get_input_text() == "[Pasted text #1 +1 lines]"
    assert tui._paste_entries[0]["expanded"] == "line1\nline2"
    assert tui._queue.empty()
