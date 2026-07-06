from pathlib import Path
import sys


from voidx.ui.output.dock import formatting


def teardown_function():
    formatting._FORMAT_CONSOLES.clear()


def test_strip_ansi_trailing_space_reuses_console(monkeypatch):
    formatting._FORMAT_CONSOLES.clear()
    created = 0
    original_console = formatting.Console

    class CountingConsole(original_console):
        def __init__(self, *args, **kwargs):
            nonlocal created
            created += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(formatting, "Console", CountingConsole)

    formatting._strip_ansi_trailing_space("\x1b[32mhello  \x1b[0m")
    formatting._strip_ansi_trailing_space("\x1b[31mworld  \x1b[0m")

    assert created == 1


def test_markdown_lines_reuses_console_by_width(monkeypatch):
    formatting._FORMAT_CONSOLES.clear()
    created = 0
    original_console = formatting.Console

    class CountingConsole(original_console):
        def __init__(self, *args, **kwargs):
            nonlocal created
            created += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(formatting, "Console", CountingConsole)

    formatting._markdown_lines("**hello**", 80)
    formatting._markdown_lines("**world**", 80)

    assert created == 2


# --- split_pasted_segments ---

def test_split_pasted_segments_no_tags():
    result = formatting.split_pasted_segments("hello world")
    assert result == [(False, "hello world")]


def test_split_pasted_segments_single_block():
    text = "fix\n<pasted>\ncode\n</pasted>\npls"
    result = formatting.split_pasted_segments(text)
    assert result == [
        (False, "fix\n"),
        (True, "code"),
        (False, "\npls"),
    ]


def test_split_pasted_segments_multiple_blocks():
    text = "a\n<pasted>\nb\n</pasted>\nc\n<pasted>\nd\n</pasted>\ne"
    result = formatting.split_pasted_segments(text)
    assert result == [
        (False, "a\n"),
        (True, "b"),
        (False, "\nc\n"),
        (True, "d"),
        (False, "\ne"),
    ]


def test_split_pasted_segments_empty_content():
    text = "<pasted>\n\n</pasted>"
    result = formatting.split_pasted_segments(text)
    assert result == [(True, "")]


def test_split_pasted_segments_unclosed_tag():
    text = "fix\n<pasted>\ncode\npls"
    result = formatting.split_pasted_segments(text)
    assert result == [(False, text)]


def test_split_pasted_segments_block_at_start():
    text = "<pasted>\ncode\n</pasted>\npls"
    result = formatting.split_pasted_segments(text)
    assert result == [
        (True, "code"),
        (False, "\npls"),
    ]
