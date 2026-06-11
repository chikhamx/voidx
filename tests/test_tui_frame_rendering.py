from tests.tui_helpers import *  # noqa: F403

import os
import re
import shutil
import sys

from rich.console import Console

from voidx.ui.output.dock import dock

def test_render_frame_uses_absolute_positioning_to_avoid_scrollback_pollution(
    tmp_path, monkeypatch
):
    """Each frame render MUST use absolute cursor positioning so the
    terminal does not scroll while writing the frame.  Relative
    positioning (\x1b[{N}A) pushes old frames into scrollback because
    when the frame grows, \n at the last line triggers a scroll."""

    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil, "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=30, _environ={})

    tui._render_frame()

    text = fake_stdout.text
    # The first cursor movement (before \x1b[J, the clear-screen) MUST
    # be absolute positioning: \x1b[{row};{col}H
    clear_pos = text.find("\x1b[J")
    assert clear_pos > 0, f"Expected \\x1b[J, got: {text!r}"

    before_clear = text[:clear_pos]
    assert re.search(r"\x1b\[\d+;\d+H", before_clear), (
        f"Expected absolute \\x1b[{{row}};{{col}}H before \\x1b[J,"
        f" got: {before_clear!r}"
    )

    match = re.search(r"\x1b\[(\d+);(\d+)H", before_clear)
    row = int(match.group(1))
    # When content fits the terminal, the frame starts at row 1 (top-aligned).
    # When content exceeds terminal height, it anchors near the bottom.
    assert row >= 1, f"Frame start row {row} is invalid"
    assert row <= 30, f"Frame start row {row} exceeds terminal height 30"


def test_render_frame_starts_below_short_committed_history(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=30, _environ={})
    for index in range(3):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"committed line {index}",
            collapsed=False,
        )
    tui._committed_line_count = 3
    tui._visible_committed_rows = 3

    tui._render_frame()

    assert tui._last_frame_start_row == 4
    assert fake_stdout.text.startswith("\x1b[4;1H\x1b[J")


def test_render_frame_scrolls_visible_committed_history_before_overlap(
    tmp_path, monkeypatch
):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    for index in range(3):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"committed line {index}",
            collapsed=False,
        )
    for index in range(7):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"active line {index}",
            collapsed=False,
        )
    tui._committed_line_count = 3
    tui._visible_committed_rows = 3

    tui._render_frame()

    assert tui._last_frame_start_row == 3
    assert tui._visible_committed_rows == 2
    clear_pos = fake_stdout.text.find("\x1b[J")
    assert fake_stdout.text[:clear_pos].startswith("\x1b[12;1H\n\x1b[3;1H")


def test_flush_committed_does_not_pad_short_history_to_bottom(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=fake_stdout, force_terminal=True, width=80, height=30, _environ={})
    for index in range(3):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"committed line {index}",
            collapsed=False,
        )

    tui._flush_committed(force=True)

    assert tui._committed_line_count == 3
    assert tui._visible_committed_rows == 3
    assert fake_stdout.text.count("\n") < 10


def test_render_frame_pins_to_bottom_after_history_fills_terminal(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    for index in range(20):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"committed line {index}",
            collapsed=False,
        )
    tui._committed_line_count = 20
    tui._visible_committed_rows = 12

    tui._render_frame()

    assert tui._last_frame_start_row == max(12 - tui._last_frame_rows + 1, 1)
    assert tui._last_frame_start_row < tui._committed_line_count + 1


def test_render_frame_clips_long_transcript_to_terminal_height(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil, "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    for index in range(50):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"frame line {index:02d}",
            collapsed=False,
        )

    tui._render_frame()

    assert tui._last_frame_rows <= 12
    assert "frame line 41" in fake_stdout.text
    assert "frame line 40" not in fake_stdout.text


def test_render_frame_clips_single_wrapped_transcript_line_to_terminal_height(
    tmp_path, monkeypatch
):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="long " + ("x" * 2000),
        collapsed=False,
    )

    tui._render_frame()

    assert tui._last_frame_rows <= 12
    assert "❯" in fake_stdout.text


def test_render_frame_clips_long_choice_panel_to_terminal_height(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._busy = True
    tui._busy_started_at = 0.0
    tui._busy_activity_verb = "Ruminating"
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    tui._active_choice = [
        ("Approve", "approved", "Proceed with this plan"),
        ("Modify scope", "modified", "Approve with changes to scope"),
        ("Reject", "rejected", "Do not proceed"),
    ]
    tui._choice_prompt = (
        "Plan: 新增 `load_doc_template` 工具，让 LLM 在 design-doc 节点激活时能按需读取文档模板。\n\n"
        "Steps:\n"
        "1. 新增 src/voidx/tools/load_doc_template.py，用 importlib.resources 读取 "
        "voidx.data/templates/{doc_type}.md，返回模板内容。\n"
        "2. 在 orchestrator 的 AgentDef.tools 列表中加入 load_doc_template。\n"
        "3. 更新 design-doc 节点 step 4 的描述。\n"
        "4. 添加测试。\n"
        "5. 跑测试确认无回归。\n\n"
        "Affected files: src/voidx/tools/load_doc_template.py, "
        "src/voidx/agent/agents.py, tests/test_tools/test_basic.py"
    )
    dock.begin_capture()
    dock.ensure_agent()
    for index in range(4):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"active line {index}",
            collapsed=False,
        )

    tui._render_frame()

    assert tui._last_frame_rows <= 12
    assert "Approve" in fake_stdout.text
    assert "Reject" in fake_stdout.text


def test_input_display_rows_uses_frame_width_boundary(tmp_path):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    width = tui._frame_width()
    max_first_line_cells = width - tui._input_line_prefix_width(0) - 1

    tui._input_lines = ["x" * max_first_line_cells]
    tui._cursor_col = max_first_line_cells
    assert tui._input_display_rows(width) == [1]

    tui._input_lines = ["x" * (max_first_line_cells + 1)]
    tui._cursor_col = max_first_line_cells + 1
    assert tui._input_display_rows(width) == [2]


def test_wrapped_input_keeps_prompt_on_first_content_row(tmp_path):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._input_lines = ["x" * 80]
    tui._cursor_col = 80

    ansi = tui._capture_renderable(tui._render_bottom_impl(), tui._frame_width())
    plain_lines = [
        re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line).rstrip()
        for line in ansi.splitlines()
    ]

    assert plain_lines[1].startswith("❯ x")


def test_non_tty_flush_prints_transcript_without_live_frame_chrome(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(file=None, force_terminal=False, width=80, height=24, _environ={})
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="startup",
        header="[bold]Welcome[/bold]",
        body_lines=["plain line"],
        collapsed=False,
    )

    tui._flush_committed(force=True)

    assert "Welcome" in fake_stdout.text
    assert "plain line" in fake_stdout.text
    assert "─" not in fake_stdout.text
    assert "❯ " not in fake_stdout.text

    fake_stdout.text = ""
    tui._render_frame()

    assert fake_stdout.text == ""


def test_typing_redraws_input_region_without_rewriting_transcript(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            pass

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil, "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="startup banner",
        collapsed=False,
    )

    tui._render_frame()
    assert "startup banner" in fake_stdout.text

    fake_stdout.text = ""
    assert tui._process_input(b"x") is True
    tui._render_after_input()

    assert "startup banner" not in fake_stdout.text
    assert "x" in fake_stdout.text
