import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.text import Text


from voidx.bootstrap.skills import build_skills_api_provider
from voidx.config import Settings
from voidx.presentation.commands import COMMANDS
from voidx.presentation.output.dock import BottomInputDock, set_dock
from voidx_cli import PureTui


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
    tui = PureTui(status, commands or COMMANDS)
    tui.set_skills_api_provider(
        build_skills_api_provider(workspace, Settings(workspace))
    )
    return tui


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



def _append_previewed_file_diff(
    presentation_dock,
    tool,
    marker: str,
    *,
    path: str = "src/example.py",
):
    presentation_dock.append_file_change(
        f"""--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 existing
+{marker}
""",
        parent=tool,
        tool_call_id=tool.tool_call_id,
        preview_hunks=1,
        preview_lines=1,
    )
    diff_node = next(
        node
        for node in tool.parent.children
        if node.payload.get("diff_result")
        and node.payload.get("result_tool_call_id") == tool.tool_call_id
    )
    full_diff = next(
        child
        for child in diff_node.children
        if child.payload.get("full_diff_result")
    )
    full_diff.collapsed = False
    presentation_dock.tree.mark_dirty()
    return diff_node


__all__ = [
    "setup_dock",
    "_rich_plain",
    "_tui",
    "_render_lines",
    "_styles_covering",
    "_append_previewed_file_diff",
    "_FakeStdout",
]
