import asyncio
import contextlib
import os
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.llm.usage import UsageStats
from voidx.ui.tools.clipboard_image import ClipboardImageResult
from voidx.ui.tools.clipboard_text import ClipboardTextResult
from voidx.ui.commands import COMMANDS
from voidx.ui.output.dock import BottomInputDock, dock, set_dock
import voidx.ui.tui.terminal_mixin as terminal_mixin
from voidx.ui.tui import (
    PureTui,
    _ENTER_TERMINAL_SEQUENCE,
    _EXIT_TERMINAL_SEQUENCE,
    _rendered_row_count,
)
from voidx.ui.tui.state import InputState, RenderState


def _rich_plain(line: str) -> str:
    return Text.from_markup(line).plain


@pytest.fixture(autouse=True)
def setup_dock():
    set_dock(BottomInputDock())
    yield
    set_dock(None)


def _tui(tmp_path: Path | None = None, *, commands: list[tuple[str, str]] | None = None) -> PureTui:
    workspace = str(tmp_path) if tmp_path is not None else "/tmp/workspace"
    status = SimpleNamespace(workspace=workspace)
    return PureTui(status, commands or COMMANDS)


def _render_lines(tui: PureTui, *, width: int = 100) -> list[str]:
    console = Console(file=None, force_terminal=False, width=width, height=24, _environ={})
    with console.capture() as capture:
        console.print(tui._render_impl())
    return [line.rstrip() for line in capture.get().splitlines()]


def _styles_covering(text: Text, needle: str) -> list[str]:
    assert needle in text.plain
    start = text.plain.index(needle)
    end = start + len(needle)
    return [
        str(span.style)
        for span in text.spans
        if span.start <= start and span.end >= end
    ]


class _FakeStdout:
    def __init__(self) -> None:
        self.text = ""

    def write(self, value: str) -> int:
        self.text += value
        return len(value)

    def flush(self) -> None:
        pass



__all__ = [
    "asyncio",
    "contextlib",
    "os",
    "re",
    "shutil",
    "sys",
    "Path",
    "SimpleNamespace",
    "pytest",
    "cell_len",
    "Console",
    "Text",
    "UsageStats",
    "ClipboardImageResult",
    "ClipboardTextResult",
    "COMMANDS",
    "BottomInputDock",
    "dock",
    "set_dock",
    "terminal_mixin",
    "PureTui",
    "_ENTER_TERMINAL_SEQUENCE",
    "_EXIT_TERMINAL_SEQUENCE",
    "_rendered_row_count",
    "InputState",
    "RenderState",
    "setup_dock",
    "_rich_plain",
    "_tui",
    "_render_lines",
    "_styles_covering",
    "_FakeStdout",
]
