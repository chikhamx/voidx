"""Shared fixtures and helpers for test_ui/gateway tests."""

import re
import sys
from pathlib import Path

import pytest
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from voidx.ui.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, ui_events


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _plain(line: str) -> str:
    return _ANSI_RE.sub("", line.replace(ANSI_LINE_PREFIX, ""))


def _rich_plain(line: str) -> str:
    return Text.from_markup(_plain(line)).plain


def _tree_nodes(root):
    nodes = [root]
    for child in root.children:
        nodes.extend(_tree_nodes(child))
    return nodes


@pytest.fixture
def isolated_dock():
    test_dock = BottomInputDock()
    set_dock(test_dock)
    try:
        yield test_dock
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


