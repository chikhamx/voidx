from pathlib import Path
import sys


from voidx.presentation.output.dock import formatting


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
