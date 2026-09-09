


from tui_helpers import *  # noqa: F403

import asyncio
import os
import re
import shutil
import sys

import pytest

from rich.console import Console
from rich.text import Text

from voidx_cli.helpers import _rendered_row_count
from voidx.presentation.output.dock import BottomInputDock, dock


def test_render_frame_collects_each_region_once_per_frame(tmp_path, monkeypatch):
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
        lambda fallback=None: os.terminal_size((80, 24)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._busy = True
    tui._busy_started_at = 0.0
    tui._busy_activity_verb = "Thinking"
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    calls = {
        "status": 0,
        "panel": 0,
        "busy": 0,
        "thinking": 0,
        "input_rows": 0,
        "input_elements": 0,
        "bottom": 0,
    }

    def render_status():
        calls["status"] += 1
        return [Text("status")]

    def render_panel(width):
        calls["panel"] += 1
        return ["[bold]panel[/bold]"]

    def render_busy(width):
        calls["busy"] += 1
        return [Text("busy")]

    def render_thinking(width):
        calls["thinking"] += 1
        return [Text("thinking")]

    original_input_rows = tui._input_display_rows

    def render_input_rows(width):
        calls["input_rows"] += 1
        return original_input_rows(width)

    original_input_elements = tui._render_input_elements

    def render_input_elements(width):
        calls["input_elements"] += 1
        return original_input_elements(width)

    original_bottom = tui._render_bottom_elements

    def render_bottom(*args, **kwargs):
        calls["bottom"] += 1
        return original_bottom(*args, **kwargs)

    monkeypatch.setattr(tui, "_render_hint_lines", render_status)
    monkeypatch.setattr(tui, "_render_panel_lines", render_panel)
    monkeypatch.setattr(tui, "_render_busy_activity_elements", render_busy)
    monkeypatch.setattr(tui, "_active_thinking_stream_elements", render_thinking)
    monkeypatch.setattr(tui, "_input_display_rows", render_input_rows)
    monkeypatch.setattr(tui, "_render_input_elements", render_input_elements)
    monkeypatch.setattr(tui, "_render_bottom_elements", render_bottom)

    tui._render_frame()

    assert calls == {
        "status": 1,
        "panel": 1,
        "busy": 1,
        "thinking": 1,
        "input_rows": 1,
        "input_elements": 1,
        "bottom": 1,
    }


def test_render_frame_failure_does_not_recollect_regions(tmp_path, monkeypatch):
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
        lambda fallback=None: os.terminal_size((80, 24)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    calls = 0

    def fail_status():
        nonlocal calls
        calls += 1
        raise RuntimeError("status failed")

    monkeypatch.setattr(tui, "_render_hint_lines", fail_status)

    tui._render_frame()

    assert calls == 1
    assert "Render error: status failed" in fake_stdout.text

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
    if sys.platform != "win32":
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
    tui._active_choice = [("approved", "approved", ""), ("modified", "modified", ""), ("rejected", "rejected", "")]
    tui._choice_prompt = (
        "Plan: 新增 `document` 工具，让 LLM 在 design 节点激活时能按需读取文档模板。\n\n"
        "Steps:\n"
        "1. 新增 src/voidx/tools/load_doc_template.py，用 importlib.resources 读取 "
        "voidx.data/templates/{doc_type}.md，返回模板内容。\n"
        "2. 在 orchestrator 的 AgentDef.tools 列表中加入 document。\n"
        "3. 更新 design 节点 step 4 的描述。\n"
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

    assert tui._last_frame_rows <= (13 if sys.platform == "win32" else 12)
    assert "approved" in fake_stdout.text
    assert "rejected" in fake_stdout.text


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

    prompt_line = next((l for l in plain_lines if l.startswith("❯")), "")
    if sys.platform != "win32":
        assert prompt_line.startswith("❯ x")


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



@pytest.mark.parametrize("interaction", ["clarify", "checkpoint"])
def test_interactive_prompt_flushes_only_final_version(
    interaction, tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(file=None, force_terminal=False, width=80, height=24, _environ={})
    dock.start_turn("demo")
    if interaction == "clarify":
        dock.show_clarify("cl_1", "Which approach?", ["implement", "document"])
        prompt_text = "voidx clarify"
        detail_text = "Question: Which approach?"
    else:
        dock.show_checkpoint(
            "cp_1",
            {"goal": "Add checkpoint node", "steps": ["Render TUI node"]},
            [],
        )
        prompt_text = "voidx plan"
        detail_text = "Plan: Add checkpoint node"

    tui._flush_committed()

    assert prompt_text not in fake_stdout.text
    assert detail_text not in fake_stdout.text

    if interaction == "clarify":
        dock.resolve_clarify("cl_1", "implement")
    else:
        dock.resolve_checkpoint(
            "cp_1",
            "approved",
            "Implement directly",
            "Implement directly",
        )
    tui._flush_committed()

    assert fake_stdout.text.count(prompt_text) == 1
    assert fake_stdout.text.count(detail_text) == 1


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


def test_transcript_viewport_conversion_does_not_scan_full_history(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    lines = [f"history line {index}" for index in range(10_000)]
    converted: list[str] = []

    def tracked_text_from_line(line: str):
        converted.append(line)
        return Text(line)

    monkeypatch.setattr(
        "voidx_cli.render_frame.text_from_line",
        tracked_text_from_line,
    )

    elements = tui._transcript_elements_for_rows(lines, width=80, row_limit=1)

    assert elements
    assert len(converted) <= 10
    assert converted[0] == lines[-1]



def test_render_impl_bounds_restored_viewport_without_full_tree_render(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=False, width=80, height=12, _environ={})
    restored = type(dock.tree)()
    for index in range(1_000):
        restored.new_node(
            parent=restored.root,
            node_type="message",
            header=f"restored line {index}",
            collapsed=False,
        )
    dock.restore_tree(restored)

    def fail_full_render(_width: int):
        raise AssertionError("full tree render must not be used for the active viewport")

    monkeypatch.setattr(dock.tree, "render", fail_full_render)

    with tui._console.capture() as capture:
        tui._console.print(tui._render_impl(height=12))

    rendered = capture.get()
    assert "restored line 999" in rendered
    assert "restored line 0" not in rendered



def test_force_flush_skips_restored_long_history(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(file=None, force_terminal=False, width=80, height=12, _environ={})
    restored = type(dock.tree)()
    for index in range(1_000):
        restored.new_node(
            parent=restored.root,
            node_type="message",
            header=f"restored line {index}",
            collapsed=False,
        )
    dock.restore_tree(restored)

    def fail_full_render(_width: int):
        raise AssertionError("restored history must not be flushed by full render")

    monkeypatch.setattr(dock.tree, "render", fail_full_render)
    tui._flush_committed(force=True)

    assert tui._committed_line_count == 0



def test_restore_tree_clears_stale_force_flush_after_reset():
    dock.reset()
    restored = type(dock.tree)()
    restored.new_node(
        parent=restored.root,
        node_type="message",
        header="restored history",
        collapsed=False,
    )

    dock.restore_tree(restored, append=True)

    assert dock.consume_clear_screen_request() is True
    assert dock.consume_force_flush_request() is False



def test_restored_history_flushes_new_output_without_replaying_history(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(file=None, force_terminal=False, width=80, height=12, _environ={})
    restored = type(dock.tree)()
    for index in range(1_000):
        restored.new_node(
            parent=restored.root,
            node_type="message",
            header=f"restored line {index}",
            collapsed=False,
        )
    dock.restore_tree(restored)
    tui._flush_committed(force=True)

    dock.append_message("new output")
    monkeypatch.setattr(
        dock.tree,
        "render",
        lambda _width: (_ for _ in ()).throw(
            AssertionError("restored flush must not use full render")
        ),
    )
    tui._flush_committed(force=True)

    assert "new output" in fake_stdout.text
    fake_stdout.text = ""
    tui._flush_committed(force=True)
    assert fake_stdout.text == ""





def test_restored_file_diff_keeps_filtered_results_in_active_area(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(
        file=None,
        force_terminal=False,
        width=80,
        height=24,
        _environ={},
    )

    restored_dock = BottomInputDock()
    restored_dock.begin_capture()
    restored_dock.start_turn("restored edit")
    restored_tool = restored_dock.start_tool(
        "Editing",
        'file_path="src/restored.py"',
        tool_name="edit",
        tool_call_id="edit-restored",
        raw_args={"file_path": "src/restored.py"},
    )
    restored_dock.finish_tool_node(restored_tool, "Editing", 0.1, True)
    restored_dock.append_tool_result(
        "ordinary restored result",
        parent=restored_tool,
        tool_call_id=restored_tool.tool_call_id,
    )
    _append_previewed_file_diff(
        restored_dock,
        restored_tool,
        "restored diff",
        path="src/restored.py",
    )

    dock.begin_capture()
    dock.restore_tree(restored_dock.tree)
    restored_active = "\n".join(_render_lines(tui))
    assert "ordinary restored result" in restored_active

    dock.start_turn("new edit")
    new_tool = dock.start_tool(
        "Editing",
        'file_path="src/new.py"',
        tool_name="edit",
        tool_call_id="edit-new",
        raw_args={"file_path": "src/new.py"},
    )
    dock.finish_tool_node(new_tool, "Editing", 0.1, True)
    dock.append_tool_result(
        "ordinary new result",
        parent=new_tool,
        tool_call_id=new_tool.tool_call_id,
    )
    _append_previewed_file_diff(
        dock,
        new_tool,
        "new diff",
        path="src/new.py",
    )

    tui._flush_committed(force=True)

    output = Text.from_ansi(fake_stdout.text).plain
    assert 'Update("src/new.py")' in output
    assert "new diff" in output
    assert "ordinary new result" not in output
    active = "\n".join(_render_lines(tui))
    assert "ordinary new result" in active



def test_resume_restore_does_not_replay_history_after_new_output(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})

    for text in ("history A", "history B", "history C"):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=text,
            collapsed=False,
        )
    tui._flush_committed(force=True)
    fake_stdout.text = ""

    restored = type(dock.tree)()
    for text in ("history A", "history B", "history C"):
        restored.new_node(
            parent=restored.root,
            node_type="message",
            header=text,
            collapsed=False,
        )
    dock.reset()
    dock.restore_tree(restored, append=True)
    tui._render_frame()

    dock.start_turn("new user")
    dock.append_message("new ai message")
    tui._flush_committed()
    tui._render_frame()

    rendered = fake_stdout.text
    new_user_offset = rendered.index("new user")
    clear_offset = rendered.rfind("\x1b[J", 0, new_user_offset)
    visible_output = rendered[clear_offset:]
    assert visible_output.index("history C") < visible_output.index("new user")
    assert visible_output.index("new user") < visible_output.index("new ai message")
    assert visible_output.count("history C") == 1




def test_startup_restore_keeps_history_when_new_output_is_flushed(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="startup",
        header="startup banner",
        collapsed=False,
    )
    restored = type(dock.tree)()
    restored.new_node(
        parent=restored.root,
        node_type="message",
        header="restored history",
        collapsed=False,
    )
    dock.restore_tree(restored, append=True)

    tui._flush_committed(force=True)
    tui._render_frame()
    dock.start_turn("new user")
    dock.append_message("new ai message")
    tui._flush_committed()

    new_user_offset = fake_stdout.text.index("new user")
    clear_offset = fake_stdout.text.rfind("\x1b[J", 0, new_user_offset)
    history_offset = fake_stdout.text.find("restored history", clear_offset)
    assert history_offset != -1
    assert history_offset < new_user_offset


def test_resume_does_not_retire_history_before_first_frame(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    restored = type(dock.tree)()
    restored.new_node(
        parent=restored.root,
        node_type="message",
        header="restored history",
        collapsed=False,
    )
    tui._render_frame()
    dock.reset()
    dock.restore_tree(restored, append=True)
    dock.start_turn("new user")
    dock.append_message("new ai message")

    tui._flush_committed()

    assert tui._restored_history_retired is False

    tui._render_frame()
    assert "restored history" in fake_stdout.text


def test_resume_stream_stays_live_after_restored_assistant_tool_history(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})

    restored = type(dock.tree)()
    turn = restored.new_node(
        parent=restored.root,
        node_type="turn",
        header="[bold white]❯[/] restored question",
        collapsed=False,
        payload={
            "transcript_turn_id": 1,
            "lifecycle": "completed",
            "active": False,
            "terminal": True,
            "committed": True,
            "durable": True,
            "referenced": False,
            "pinned": False,
            "render_pending": False,
        },
    )
    agent = restored.new_node(
        parent=turn,
        node_type="assistant",
        header="",
        collapsed=False,
    )
    restored.new_node(
        parent=agent,
        node_type="assistant",
        header="● restored answer",
        collapsed=False,
    )
    tool = restored.new_node(
        parent=agent,
        node_type="tool_call",
        header="● Read(restored.py)",
        collapsed=True,
        status="done",
    )
    restored.new_node(
        parent=tool,
        node_type="tool_result",
        header="restored tool result",
        collapsed=False,
    )

    dock.begin_capture()
    dock.restore_tree(restored)
    tui._render_frame()
    fake_stdout.text = ""

    dock.start_turn("new question")
    dock.set_stream("new streaming answer")
    tui._flush_committed()
    fake_stdout.text = ""
    tui._render_frame()

    assert "new streaming answer" in fake_stdout.text



def test_resume_from_empty_tree_commits_history_before_first_input(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    restored = type(dock.tree)()
    restored.new_node(
        parent=restored.root,
        node_type="message",
        header="restored history",
        collapsed=False,
    )

    dock.reset()
    dock.restore_tree(restored, append=True)
    dock.append_message("Resumed session")
    fake_stdout.text = ""

    tui._run_scheduled_render()

    assert fake_stdout.text.index("restored history") < fake_stdout.text.index(
        "Resumed session"
    )
    assert dock.restored_root_child_range() is None

    fake_stdout.text = ""
    assert tui._process_input(b"x") is True
    tui._render_after_input()

    assert "restored history" not in fake_stdout.text
    assert "x" in fake_stdout.text




def test_thinking_stream_lookup_uses_only_stream_node_subtree(monkeypatch):
    dock.begin_capture()
    dock.start_turn("inspect")
    dock.set_stream("thinking now", phase="thinking")

    def fail_full_line_map(_width: int):
        raise AssertionError("thinking lookup must not render the full tree")

    monkeypatch.setattr(dock.tree, "render_with_line_map", fail_full_line_map)
    lines = dock.active_thinking_stream_lines(80)

    assert any("thinking now" in line for line in lines)



def test_panel_viewport_conversion_does_not_scan_full_history(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._panel_row_limit = 5
    lines = [f"[dim]panel line {index}[/dim]" for index in range(1_000)]
    converted: list[str] = []

    def tracked_text_from_line(line: str):
        converted.append(line)
        return Text.from_markup(line)

    monkeypatch.setattr(
        "voidx_cli.render_frame.text_from_line",
        tracked_text_from_line,
    )

    elements = tui._render_panel_elements(lines, width=80)

    assert elements
    assert len(elements) <= 5
    assert len(converted) <= 13
    assert converted[0] == lines[-1]


def test_render_impl_bounds_panel_conversion_before_final_capture(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=False, width=80, height=12, _environ={})
    lines = [f"[dim]panel line {index}[/dim]" for index in range(1_000)]
    converted: list[str] = []

    def tracked_text_from_line(line: str):
        converted.append(line)
        return Text.from_markup(line)

    monkeypatch.setattr(tui, "_render_panel_lines", lambda _width: lines)
    monkeypatch.setattr(
        "voidx_cli.render_frame.text_from_line",
        tracked_text_from_line,
    )

    tui._render_impl(height=12)

    assert converted
    assert len(converted) <= 20
    assert converted[0] == lines[-1]


def test_panel_viewport_markup_failure_falls_back_without_truncating_text(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._panel_row_limit = 5
    lines = ["[bold]safe[/bold]", "[not-closed"]

    def fail_markup(line: str):
        if line == lines[1]:
            raise ValueError("invalid markup")
        return Text.from_markup(line)

    monkeypatch.setattr(
        "voidx_cli.render_frame.text_from_line",
        fail_markup,
    )

    elements = tui._render_panel_elements(lines, width=80)
    rendered = "\n".join(element.plain for element in elements)

    assert "safe" in rendered
    assert "[not-closed" in rendered


def test_busy_activity_tick_reuses_last_full_render_plan_geometry(tmp_path, monkeypatch):
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
        lambda fallback=None: os.terminal_size((80, 24)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._busy = True
    tui._busy_started_at = 0.0
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    calls = {"status": 0, "panel": 0, "thinking": 0, "input": 0}
    original_input_rows = tui._input_display_rows

    def render_status():
        calls["status"] += 1
        return [Text("status")]

    def render_panel(width):
        calls["panel"] += 1
        return ["[bold]panel[/bold]"]

    def render_thinking(width):
        calls["thinking"] += 1
        return [Text("thinking")]

    def render_input_rows(width):
        calls["input"] += 1
        return original_input_rows(width)

    monkeypatch.setattr(tui, "_render_hint_lines", render_status)
    monkeypatch.setattr(tui, "_render_panel_lines", render_panel)
    monkeypatch.setattr(tui, "_active_thinking_stream_elements", render_thinking)
    monkeypatch.setattr(tui, "_input_display_rows", render_input_rows)

    tui._render_frame()
    assert calls == {"status": 1, "panel": 1, "thinking": 1, "input": 1}

    assert tui._render_busy_activity_tick() is True
    assert calls == {"status": 1, "panel": 1, "thinking": 1, "input": 1}


def test_choice_selection_repaint_invalidates_last_render_plan(tmp_path, monkeypatch):
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
        lambda fallback=None: os.terminal_size((80, 24)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._has_rendered_frame = True
    tui._last_bottom_start_row = 7
    tui._last_frame_rows = 14
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._active_choice = [("review", "review", ""), ("implement", "implement", "")]
    tui._choice_prompt = "Intent?"
    tui._choice_selected = 0
    ansi = tui._capture_renderable(tui._render_bottom_impl(), tui._frame_width())
    tui._last_bottom_rows = _rendered_row_count(ansi)
    tui._last_render_plan = object()

    assert tui._render_choice_selection_region() is True
    assert tui._last_render_plan is None




def test_choice_close_forces_full_frame_repaint_before_next_paint(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    for index in range(8):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"stable history {index}",
            collapsed=False,
        )
    tui._active_choice = [
        ("resume one", "one", "first session"),
        ("resume two", "two", "second session"),
    ]
    tui._choice_prompt = "Resume session?"
    tui._choice_selected = 0

    tui._render_frame()
    fake_stdout.text = ""
    tui._clear_choice_prompt()

    def fail_diff(*_args, **_kwargs):
        raise AssertionError("closing a choice must force a complete frame repaint")

    monkeypatch.setattr(tui, "_render_diff", fail_diff)
    tui._render_frame()

    assert tui._render_stats.strategy == "full"
    assert "\x1b[J" in fake_stdout.text
    assert "Resume session?" not in fake_stdout.text


def test_worker_choice_close_forces_full_frame_repaint(tmp_path, monkeypatch):
    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    tui._active_choice = [
        ("resume one", "one", "first session"),
        ("resume two", "two", "second session"),
    ]
    tui._choice_prompt = "Resume session?"
    tui._choice_selected = 0

    tui._render_frame()
    assert "Resume session?" in "\n".join(writer.frames[0].target_lines)

    tui._clear_choice_prompt()
    tui._render_frame()

    assert len(writer.frames) == 2
    assert writer.frames[1].force_full is True
    assert "Resume session?" not in "\n".join(writer.frames[1].target_lines)


class _WorkerFrameWriter:
    worker_mode = True

    def __init__(self) -> None:
        self.frames = []
        self.barriers = []
        self.events = []
        self.frame_error = None
        self.barrier_error = None
        self.barrier_hook = None

    def submit_frame(self, batch) -> None:
        if self.frame_error is not None:
            raise self.frame_error
        self.frames.append(batch)
        self.events.append(("frame", batch.generation))

    def submit_barrier(self, **kwargs):
        if self.barrier_hook is not None:
            self.barrier_hook(kwargs)
        if self.barrier_error is not None:
            raise self.barrier_error
        self.barriers.append(kwargs)
        self.events.append(("barrier", kwargs["kind"]))
        return object()

    def write(self, value: str) -> int:
        raise AssertionError(f"worker render used synchronous write: {value!r}")

    def flush(self) -> None:
        raise AssertionError("worker render used synchronous flush")


def test_worker_render_failure_does_not_recollect_regions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _WorkerFrameWriter()
    tui._terminal_writer = writer
    calls = 0

    def fail_status():
        nonlocal calls
        calls += 1
        raise RuntimeError("status failed")

    monkeypatch.setattr(tui, "_render_hint_lines", fail_status)

    tui._render_frame()

    assert calls == 1
    assert len(writer.frames) == 1
    assert "Render error: status failed" in "\n".join(writer.frames[0].target_lines)
    assert writer.frames[0].cursor_ansi == ""


def test_worker_render_enqueues_atomic_frame_and_accepts_only_latest_stats(
    tmp_path, monkeypatch
):
    from voidx_cli.terminal_writer import FrameResult

    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _WorkerFrameWriter()
    tui._terminal_writer = writer

    assert tui._terminal_frame_generation == 0
    assert tui._terminal_writer_failed is False

    tui._render_frame()

    assert tui._terminal_frame_generation == 1
    assert len(writer.frames) == 1
    batch = writer.frames[0]
    assert batch.generation == 1
    assert isinstance(batch.target_lines, tuple)
    assert batch.target_lines
    assert re.fullmatch(r"\x1b\[\d+;\d+H", batch.cursor_ansi)
    assert batch.render_ms >= 0
    assert tui._render_stats is None

    tui._handle_terminal_frame_result(
        FrameResult(0, 99, 99, 99.0, "stale", True)
    )
    tui._handle_terminal_frame_result(
        FrameResult(1, 99, 99, 99.0, "stale", False)
    )
    assert tui._render_stats is None

    tui._handle_terminal_frame_result(
        FrameResult(1, 7, 2, 1.5, "diff", True)
    )
    assert tui._render_stats.total_lines == 7
    assert tui._render_stats.changed_lines == 2
    assert tui._render_stats.render_ms == 1.5
    assert tui._render_stats.strategy == "diff"


def test_worker_input_and_choice_repaints_fall_back_to_full_frame(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._terminal_writer = _WorkerFrameWriter()
    tui._has_rendered_frame = True
    tui._last_bottom_rows = 1
    tui._last_bottom_start_row = 7
    tui._last_frame_rows = 10
    calls = []
    monkeypatch.setattr(tui, "_render_frame", lambda: calls.append("frame"))

    tui._render_input_region()
    assert calls == ["frame"]

    tui._active_choice = [("one", "one", "")]
    assert tui._render_choice_selection_region() is True
    assert calls == ["frame", "frame"]


def test_worker_busy_tick_falls_back_to_full_frame(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._terminal_writer = _WorkerFrameWriter()
    tui._has_rendered_frame = True
    tui._render_scheduled = False
    calls = []
    monkeypatch.setattr(tui, "_busy_activity_tick_active", lambda: True)
    monkeypatch.setattr(tui, "_frame_geometry_changed", lambda: False)
    monkeypatch.setattr(tui, "_render_frame", lambda: calls.append("frame"))

    assert tui._render_busy_activity_tick() is True
    assert calls == ["frame"]


def _worker_render_tui(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _WorkerFrameWriter()
    tui._terminal_writer = writer
    return tui, writer


def test_worker_frame_projects_oversized_bottom_into_physical_viewport(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 6)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=6, _environ={})
    writer = _WorkerFrameWriter()
    tui._terminal_writer = writer
    tui._input_lines = [f"input {index}" for index in range(10)]
    tui._cursor_row = 9
    tui._cursor_col = len(tui._input_lines[-1])

    tui._render_frame()

    assert len(writer.frames) == 1
    batch = writer.frames[0]
    assert len(batch.target_lines) <= 6
    assert batch.start_row >= 1
    assert batch.start_row + len(batch.target_lines) - 1 <= 6
    snapshot = tui._pending_layout_snapshots[batch.generation]
    assert snapshot.frame_rows == len(batch.target_lines)
    assert 1 <= snapshot.cursor_row <= 6
    assert snapshot.bottom.region.start_row <= snapshot.cursor_row
    assert snapshot.bottom.region.start_row + snapshot.bottom.region.visual_rows - 1 <= 6


def test_worker_clear_request_submits_barrier_before_forced_frame(tmp_path, monkeypatch):
    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    dock.reset()

    tui._render_frame()

    assert writer.barriers == [{"kind": "clear", "ansi": "\x1b[2J\x1b[H"}]
    assert len(writer.frames) == 1
    assert writer.frames[0].force_full is True
    assert writer.events == [("barrier", "clear"), ("frame", 1)]


def test_worker_resize_submits_barrier_before_forced_frame(tmp_path, monkeypatch):
    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    tui._prev_frame_width = 60
    tui._prev_frame_term_height = 24

    tui._render_frame()

    assert writer.barriers == [{"kind": "resize"}]
    assert len(writer.frames) == 1
    assert writer.frames[0].force_full is True
    assert writer.events == [("barrier", "resize"), ("frame", 1)]


def test_worker_scroll_submits_barrier_without_synchronous_write(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    writer = _WorkerFrameWriter()
    tui._terminal_writer = writer
    tui._visible_committed_rows = 5

    assert tui._make_room_for_frame(frame_rows=8, term_height=10) is True
    assert writer.barriers == [
        {
            "kind": "scroll",
            "ansi": "\x1b[10;1H" + "\n" * 3,
        }
    ]
    assert tui._visible_committed_rows == 2




@pytest.mark.parametrize("boundary", ["resize", "clear", "scroll"])
def test_worker_terminal_boundaries_invalidate_layout_snapshot(
    tmp_path, monkeypatch, boundary
):
    if boundary == "scroll":
        tui = _tui(tmp_path)
        tui._tty = True
        writer = _WorkerFrameWriter()
        tui._terminal_writer = writer
        tui._visible_committed_rows = 5
    else:
        tui, writer = _worker_render_tui(tmp_path, monkeypatch)
        if boundary == "resize":
            tui._prev_frame_width = 60
            tui._prev_frame_term_height = 24
        else:
            dock.request_clear_screen()

    sentinel = object()
    tui._applied_layout_snapshot = sentinel
    tui._pending_layout_snapshots[999] = sentinel
    tui._pending_layout_force_full[999] = False
    original_epoch = tui._scroll_epoch

    if boundary == "scroll":
        assert tui._make_room_for_frame(frame_rows=8, term_height=10) is True
    else:
        tui._render_frame()

    assert tui._scroll_epoch == original_epoch + 1
    assert tui._applied_layout_snapshot is None
    assert 999 not in tui._pending_layout_snapshots
    assert 999 not in tui._pending_layout_force_full

def test_worker_make_room_without_scroll_preserves_layout_snapshot(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    writer = _WorkerFrameWriter()
    tui._terminal_writer = writer
    tui._visible_committed_rows = 1

    sentinel = object()
    tui._applied_layout_snapshot = sentinel
    tui._pending_layout_snapshots[999] = sentinel
    tui._pending_layout_force_full[999] = False
    original_epoch = tui._scroll_epoch

    assert tui._make_room_for_frame(frame_rows=2, term_height=10) is False

    assert tui._scroll_epoch == original_epoch
    assert tui._applied_layout_snapshot is sentinel
    assert tui._pending_layout_snapshots[999] is sentinel
    assert tui._pending_layout_force_full[999] is False
    assert writer.barriers == []



def test_worker_frame_generation_advances_only_after_successful_submit(
    tmp_path, monkeypatch
):
    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    writer.frame_error = RuntimeError("enqueue failed")

    with pytest.raises(RuntimeError, match="enqueue failed"):
        tui._render_frame()

    assert tui._terminal_frame_generation == 0
    assert writer.frames == []



def test_worker_layout_snapshot_promotes_only_after_matching_applied_result(
    tmp_path, monkeypatch
):
    from voidx_cli.layout import LayoutSnapshot
    from voidx_cli.terminal_writer import FrameResult

    tui, writer = _worker_render_tui(tmp_path, monkeypatch)

    tui._render_frame()

    assert tui._submitted_generation == 1
    assert tui._layout_generation == 1
    assert tui._applied_layout_snapshot is None
    assert set(tui._pending_layout_snapshots) == {1}
    assert isinstance(tui._pending_layout_snapshots[1], LayoutSnapshot)

    tui._handle_terminal_frame_result(FrameResult(1, 1, 1, 0.1, "diff", False))
    assert tui._applied_layout_snapshot is None
    assert set(tui._pending_layout_snapshots) == {1}

    tui._handle_terminal_frame_result(FrameResult(0, 1, 1, 0.1, "stale", True))
    assert tui._applied_layout_snapshot is None
    assert set(tui._pending_layout_snapshots) == {1}

    tui._handle_terminal_frame_result(FrameResult(1, 1, 1, 0.1, "full", True))
    assert tui._applied_layout_snapshot is not None
    assert tui._applied_layout_snapshot.generation == 1
    assert tui._pending_layout_snapshots == {}


def test_worker_frame_submit_failure_invalidates_pending_layout_snapshot(
    tmp_path, monkeypatch
):
    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    writer.frame_error = RuntimeError("enqueue failed")

    with pytest.raises(RuntimeError, match="enqueue failed"):
        tui._render_frame()

    assert tui._submitted_generation == 0
    assert tui._pending_layout_snapshots == {}
    assert tui._terminal_submission_failed is True
    assert tui._full_layout_invalidated is True


def test_input_cursor_sequence_is_pure_in_worker_mode(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._terminal_writer = _WorkerFrameWriter()
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    sequence = tui._input_cursor_sequence()

    assert sequence.startswith("\x1b[")
    assert sequence.endswith("G")
    assert tui._terminal_writer.frames == []
    assert tui._terminal_writer.barriers == []


def _worker_frame_state(tui):
    return (
        tui._has_rendered_frame,
        tui._last_frame_rows,
        tui._last_frame_start_row,
        tui._last_bottom_rows,
        tui._last_bottom_start_row,
        tui._cursor_to_frame_top_lines,
        tui._cursor_to_frame_end_lines,
        tui._prev_frame_lines,
        tui._prev_frame_start_row,
        tui._prev_frame_width,
        tui._prev_frame_term_height,
        tui._last_render_plan,
        tui._last_busy_activity_rows,
        tui._last_busy_activity_start_row,
    )


def test_worker_frame_submit_failure_preserves_previous_frame_state(tmp_path, monkeypatch):
    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    previous_plan = object()
    tui._has_rendered_frame = True
    tui._last_frame_rows = 9
    tui._last_frame_start_row = 3
    tui._last_bottom_rows = 4
    tui._last_bottom_start_row = 8
    tui._cursor_to_frame_top_lines = 2
    tui._cursor_to_frame_end_lines = 7
    tui._prev_frame_lines = ["previous"]
    tui._prev_frame_start_row = 3
    tui._prev_frame_width = tui._frame_width()
    tui._prev_frame_term_height = 24
    tui._last_render_plan = previous_plan
    tui._last_busy_activity_rows = 2
    tui._last_busy_activity_start_row = 5
    previous_state = _worker_frame_state(tui)
    writer.frame_error = RuntimeError("enqueue failed")

    with pytest.raises(RuntimeError, match="enqueue failed"):
        tui._render_frame()

    assert _worker_frame_state(tui) == previous_state
    assert tui._terminal_frame_generation == 0
    assert tui._render_plan is None


def test_worker_resize_barrier_failure_preserves_frame_cache(tmp_path, monkeypatch):
    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    previous_plan = object()
    tui._prev_frame_lines = ["previous"]
    tui._prev_frame_start_row = 4
    tui._prev_frame_width = 60
    tui._prev_frame_term_height = 24
    tui._last_render_plan = previous_plan
    previous_cache = (
        tui._prev_frame_lines,
        tui._prev_frame_start_row,
        tui._prev_frame_width,
        tui._prev_frame_term_height,
        tui._last_render_plan,
    )
    writer.barrier_error = RuntimeError("resize failed")

    with pytest.raises(RuntimeError, match="resize failed"):
        tui._render_frame()

    assert (
        tui._prev_frame_lines,
        tui._prev_frame_start_row,
        tui._prev_frame_width,
        tui._prev_frame_term_height,
        tui._last_render_plan,
    ) == previous_cache
    assert tui._render_plan is None
    assert writer.events == []


def test_worker_clear_barrier_failure_preserves_counts_and_request(tmp_path, monkeypatch):
    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    tui._committed_line_count = 7
    projection = object()
    tui._committed_projection = projection
    tui._visible_committed_rows = 5
    dock.reset()
    writer.barrier_error = RuntimeError("clear failed")

    with pytest.raises(RuntimeError, match="clear failed"):
        tui._render_frame()

    assert tui._committed_line_count == 7
    assert tui._committed_projection is projection
    assert tui._visible_committed_rows == 5
    assert dock.consume_clear_screen_request() is True
    assert tui._render_plan is None
    assert writer.events == []


@pytest.mark.parametrize("barrier_kind", ["resize", "clear", "scroll"])
def test_worker_render_ms_excludes_barrier_enqueue_time(
    tmp_path, monkeypatch, barrier_kind
):
    import voidx_cli.render_frame as render_frame_module

    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    clock = [1.0]
    monkeypatch.setattr(render_frame_module.time, "perf_counter", lambda: clock[0])
    writer.barrier_hook = lambda _barrier: clock.__setitem__(0, clock[0] + 7.0)
    if barrier_kind == "resize":
        tui._prev_frame_width = 60
        tui._prev_frame_term_height = 24
    elif barrier_kind == "clear":
        dock.reset()
    else:
        tui._visible_committed_rows = 24

    tui._render_frame()

    assert writer.events[0] == ("barrier", barrier_kind)
    assert writer.events[-1] == ("frame", 1)
    assert writer.frames[0].render_ms == 0.0


class _WorkerCommitWriter:
    worker_mode = True

    def __init__(self) -> None:
        self.commits = []
        self.commit_error = None

    def submit_commit(self, **kwargs):
        if self.commit_error is not None:
            raise self.commit_error
        self.commits.append(kwargs)
        return object()

    def write(self, value: str) -> int:
        raise AssertionError(f"worker commit used synchronous write: {value!r}")

    def flush(self) -> None:
        raise AssertionError("worker commit used synchronous flush")


def _worker_commit_tui(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _WorkerCommitWriter()
    tui._terminal_writer = writer
    return tui, writer


def test_worker_file_diff_commit_uses_scrollback_whitelist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui, writer = _worker_commit_tui(tmp_path)
    tui._has_rendered_frame = True
    tui._last_frame_start_row = 6
    dock.begin_capture()
    dock.start_turn("worker edit")
    tool = dock.start_tool(
        "Editing",
        'file_path="src/worker.py"',
        tool_name="edit",
        tool_call_id="edit-worker",
        raw_args={"file_path": "src/worker.py"},
    )
    dock.finish_tool_node(tool, "Editing", 0.1, True)
    result = dock.append_tool_result(
        "ordinary worker result",
        parent=tool,
        tool_call_id=tool.tool_call_id,
    )
    diff_node = _append_previewed_file_diff(
        dock,
        tool,
        "worker diff",
        path="src/worker.py",
    )

    tui._flush_committed(force=True)

    assert len(writer.commits) == 1
    output = Text.from_ansi(writer.commits[0]["ansi"]).plain
    assert 'Update("src/worker.py")' in output
    assert "worker diff" in output
    assert "ordinary worker result" not in output
    assert diff_node.id in dock._settled_node_ids
    assert result.id not in dock._settled_node_ids



def test_worker_flush_committed_submits_one_atomic_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui, writer = _worker_commit_tui(tmp_path)
    tui._has_rendered_frame = True
    tui._last_frame_start_row = 6
    tui._prev_frame_lines = ["active frame"]
    tui._prev_frame_width = 79
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="committed output",
        collapsed=False,
    )
    expected_count = len(dock.tree.render(tui._frame_width()))

    tui._flush_committed(force=True)

    assert len(writer.commits) == 1
    commit = writer.commits[0]
    assert commit["clear_start_row"] == 6
    assert "committed output" in commit["ansi"]
    assert commit["ansi"].endswith("\n")
    assert tui._committed_line_count == expected_count
    assert tui._visible_committed_rows > 0
    assert tui._prev_frame_lines is None


def test_worker_flush_committed_submit_failure_preserves_watermark_and_frame(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui, writer = _worker_commit_tui(tmp_path)
    writer.commit_error = RuntimeError("commit enqueue failed")
    tui._has_rendered_frame = True
    tui._last_frame_start_row = 6
    tui._visible_committed_rows = 2
    tui._prev_frame_lines = ["active frame"]
    tui._prev_frame_start_row = 6
    tui._prev_frame_width = 79
    tui._prev_frame_term_height = 24
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="must retry",
        collapsed=False,
    )
    previous_frame = (
        tui._has_rendered_frame,
        tui._visible_committed_rows,
        tui._prev_frame_lines,
        tui._prev_frame_start_row,
        tui._prev_frame_width,
        tui._prev_frame_term_height,
    )

    with pytest.raises(RuntimeError, match="commit enqueue failed"):
        tui._flush_committed(force=True)

    assert tui._committed_line_count == 0
    assert (
        tui._has_rendered_frame,
        tui._visible_committed_rows,
        tui._prev_frame_lines,
        tui._prev_frame_start_row,
        tui._prev_frame_width,
        tui._prev_frame_term_height,
    ) == previous_frame
    assert writer.commits == []


def test_worker_restored_commit_failure_preserves_state_and_output_requests(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui, writer = _worker_commit_tui(tmp_path)
    prefix = dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="existing prefix",
        collapsed=False,
    )
    restored = type(dock.tree)()
    restored.new_node(
        parent=restored.root,
        node_type="message",
        header="restored history",
        collapsed=False,
    )
    dock.restore_tree(restored, append=True)
    assert prefix in dock.tree.root.children
    tui._sync_restored_render_state()
    tui._has_rendered_frame = True
    tui._last_frame_start_row = 5
    dock.append_message("new output")
    dock.request_force_flush()
    dock.queue_guidance_echo("retry guidance")
    previous_state = (
        tui._restored_committed_line_count,
        tui._restored_startup_flushed,
        tui._restored_history_retired,
        tui._committed_line_count,
    )
    writer.commit_error = RuntimeError("commit enqueue failed")

    with pytest.raises(RuntimeError, match="commit enqueue failed"):
        tui._flush_committed()

    assert (
        tui._restored_committed_line_count,
        tui._restored_startup_flushed,
        tui._restored_history_retired,
        tui._committed_line_count,
    ) == previous_state
    assert dock.consume_force_flush_request() is True
    assert dock.consume_guidance_echoes() == ["retry guidance"]


def test_worker_restored_flush_deferral_requeues_force_request(tmp_path):
    tui, writer = _worker_commit_tui(tmp_path)
    restored = type(dock.tree)()
    restored.new_node(
        parent=restored.root,
        node_type="message",
        header="restored history",
        collapsed=False,
    )
    dock.restore_tree(restored)
    tui._sync_restored_render_state()
    dock.append_message("new output awaiting first frame")
    dock.request_force_flush()
    tui._has_rendered_frame = False

    assert tui._flush_committed() is None

    assert writer.commits == []
    assert tui._restored_committed_line_count == 0
    assert dock.consume_force_flush_request() is True


class _DeferredCommitToken:
    def __init__(self, loop):
        self.future = loop.create_future()


class _DeferredCommitWriter(_WorkerCommitWriter):
    def __init__(self):
        super().__init__()
        self.tokens = []

    def submit_commit(self, **kwargs):
        if self.commit_error is not None:
            raise self.commit_error
        import asyncio

        token = _DeferredCommitToken(asyncio.get_running_loop())
        self.commits.append(kwargs)
        self.tokens.append(token)
        return token

    async def wait(self, token):
        await token.future



class _DeferredCommitFrameWriter(_DeferredCommitWriter):
    def __init__(self):
        super().__init__()
        self.frames = []
        self.barriers = []

    def submit_frame(self, batch):
        self.frames.append(batch)

    def submit_barrier(self, **kwargs):
        self.barriers.append(kwargs)
        return object()


@pytest.mark.asyncio
async def test_worker_file_diff_settles_only_after_pending_commit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    dock.begin_capture()
    dock.start_turn("deferred worker edit")
    tool = dock.start_tool(
        "Editing",
        'file_path="src/deferred.py"',
        tool_name="edit",
        tool_call_id="edit-deferred",
        raw_args={"file_path": "src/deferred.py"},
    )
    dock.finish_tool_node(tool, "Editing", 0.1, True)
    result = dock.append_tool_result(
        "ordinary deferred result",
        parent=tool,
        tool_call_id=tool.tool_call_id,
    )
    diff_node = _append_previewed_file_diff(
        dock,
        tool,
        "deferred diff",
        path="src/deferred.py",
    )
    full_diff = next(
        child
        for child in diff_node.children
        if child.payload.get("full_diff_result")
    )

    token = tui._flush_committed(force=True)

    assert token is writer.tokens[0]
    assert diff_node.id not in dock._settled_node_ids
    assert full_diff.id not in dock._settled_node_ids
    assert result.id not in dock._settled_node_ids

    await _resolve_deferred_commit(token)

    assert diff_node.id in dock._settled_node_ids
    assert full_diff.id in dock._settled_node_ids
    assert result.id not in dock._settled_node_ids



@pytest.mark.asyncio
async def test_worker_resume_scheduled_render_commits_before_first_input(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitFrameWriter()
    tui._terminal_writer = writer
    restored = type(dock.tree)()
    restored.new_node(
        parent=restored.root,
        node_type="message",
        header="restored history",
        collapsed=False,
    )
    dock.reset()
    dock.restore_tree(restored, append=True)
    dock.append_message("Resumed session")

    tui._run_scheduled_render()

    assert len(writer.commits) == 1
    commit_ansi = writer.commits[0]["ansi"]
    assert commit_ansi.index("restored history") < commit_ansi.index("Resumed session")
    assert dock.restored_root_child_range() == (0, 1)
    assert tui._restored_history_retired is False

    frame_count = len(writer.frames)
    assert tui._process_input(b"x") is True
    tui._render_after_input()
    assert len(writer.frames) == frame_count

    writer.tokens[0].future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert dock.restored_root_child_range() is None
    tui._run_scheduled_render()

    assert len(writer.commits) == 1
    final_frame = "\n".join(writer.frames[-1].target_lines)
    assert "restored history" not in final_frame
    assert "Resumed session" not in final_frame
    assert "x" in final_frame
    tui._running = False




@pytest.mark.asyncio
async def test_worker_physical_snapshot_preserves_restore_epoch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._restore_epoch = 7
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitFrameWriter()
    tui._terminal_writer = writer
    dock.begin_capture()
    dock.append_message("restored frame")

    tui._render_frame()

    assert len(writer.frames) == 1
    snapshot = tui._pending_layout_snapshots[writer.frames[0].generation]
    assert snapshot.restore_epoch == tui._restore_epoch


@pytest.mark.asyncio
async def test_flush_after_restore_commits_before_return(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitFrameWriter()
    tui._terminal_writer = writer
    restored = type(dock.tree)()
    restored.new_node(
        parent=restored.root,
        node_type="message",
        header="restored history",
        collapsed=False,
    )

    dock.reset()
    dock.restore_tree(restored, append=True)
    dock.append_message("Resumed session")

    flush_task = asyncio.create_task(tui.flush_after_restore())
    await asyncio.sleep(0)

    assert not flush_task.done()
    assert len(writer.commits) == 1
    commit_ansi = writer.commits[0]["ansi"]
    assert commit_ansi.index("restored history") < commit_ansi.index("Resumed session")

    writer.tokens[0].future.set_result(None)
    await asyncio.wait_for(flush_task, timeout=1)

    assert dock.restored_root_child_range() is None
    assert tui._pending_commit_tokens == []


@pytest.mark.asyncio
async def test_worker_clear_keeps_startup_in_first_frame_and_first_input(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitFrameWriter()
    tui._terminal_writer = writer
    dock.append_message("old transcript")
    tui._committed_line_count = len(dock.tree.render(tui._frame_width()))

    dock.reset()
    dock.append_startup(
        model="test-model",
        provider="test-provider",
        workspace=str(tmp_path),
        session_title="New session",
        is_new=True,
    )

    token = tui._flush_committed()
    assert token is writer.tokens[0]
    tui._render_frame()
    assert writer.frames == []

    token.future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    tui._render_frame()

    first_frame = "\n".join(writer.frames[-1].target_lines)
    assert "voidx v" in first_frame
    assert "old transcript" not in first_frame

    assert tui._process_input(b"x") is True
    tui._render_after_input()

    input_frame = "\n".join(writer.frames[-1].target_lines)
    assert "voidx v" in input_frame
    assert "old transcript" not in input_frame
    assert "x" in input_frame
    tui._running = False




@pytest.mark.asyncio
async def test_worker_defers_frame_until_pending_commit_is_applied(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitFrameWriter()
    tui._terminal_writer = writer
    dock.append_message("committed exactly once")

    token = tui._flush_committed(force=True)
    tui._render_frame()

    assert writer.frames == []

    token.future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    tui._render_frame()

    assert len(writer.frames) == 1
    assert "committed exactly once" not in "\n".join(writer.frames[0].target_lines)

@pytest.mark.asyncio
async def test_worker_commit_watermark_waits_for_completed_writer_token(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    presentation_dock = dock
    presentation_dock.begin_capture()
    turn = presentation_dock.start_turn("deferred user")
    presentation_dock.append_message("deferred answer")
    presentation_dock.end_turn()
    turn.payload.update(
        durable=True,
        lifecycle="completed",
        terminal=True,
        active=False,
        referenced=False,
        pinned=False,
        render_pending=False,
    )
    for node in presentation_dock.tree.root.children:
        if node is not turn:
            node.payload.update(
                durable=True,
                committed=False,
                lifecycle="completed",
                terminal=True,
                active=False,
                referenced=False,
                pinned=False,
                render_pending=False,
            )

    token = tui._flush_committed(force=True)

    assert token is writer.tokens[0]
    assert tui._committed_line_count == 0
    assert tui._visible_committed_rows == 0

    writer.tokens[0].future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert tui._committed_line_count > 0
    assert tui._visible_committed_rows > 0






@pytest.mark.asyncio
async def test_worker_commit_does_not_reappend_stream_node_changed_before_token_completion(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    dock.begin_capture()
    dock.start_turn("user")
    text = "stable assistant marker\n\n" + "".join(
        f"- item {index} with long details\n" for index in range(20)
    )
    dock.set_stream(text, refresh=False)
    work_item = dock.prepare_stream_commit(refresh=False)
    assert work_item is not None

    from voidx.presentation.output.dock.stream import build_canonical_stream_projection

    projection = build_canonical_stream_projection(work_item)
    token = tui._flush_committed(force=True)
    assert token is writer.tokens[0]
    assert writer.commits[0]["ansi"].count("stable assistant marker") == 0

    assert dock.apply_stream_commit(work_item, projection, refresh=False) is True
    token.future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    dock.append_message("unrelated mutation")
    tui._flush_committed(force=True)

    assert len(writer.commits) == 2
    assert "unrelated mutation" in writer.commits[1]["ansi"]
    assert writer.commits[1]["ansi"].count("stable assistant marker") == 1


@pytest.mark.asyncio
async def test_worker_commit_registers_pending_operation_and_invalidates_layout(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    dock.append_message("commit operation")

    sentinel = object()
    tui._applied_layout_snapshot = sentinel
    tui._pending_layout_snapshots[7] = sentinel
    tui._pending_layout_force_full[7] = False
    original_epoch = tui._scroll_epoch

    token = tui._flush_committed(force=True)

    assert token is writer.tokens[0]
    assert tui._scroll_epoch == original_epoch + 1
    assert tui._applied_layout_snapshot is None
    assert tui._pending_layout_snapshots == {}
    assert tui._pending_layout_force_full == {}
    assert tui._full_layout_invalidated is True
    operation = tui._pending_terminal_operations[id(token)]
    assert operation["kind"] == "commit"
    assert operation["token"] is token
    assert operation["scroll_epoch"] == tui._scroll_epoch

    token.future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert tui._pending_terminal_operations == {}


@pytest.mark.asyncio
async def test_worker_commit_failure_clears_pending_operation_without_applying_state(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    dock.append_message("failed commit operation")
    tui._committed_line_count = 0
    original_epoch = tui._scroll_epoch

    token = tui._flush_committed(force=True)
    assert token is writer.tokens[0]
    assert tui._pending_terminal_operations

    token.future.set_exception(RuntimeError("commit operation failed"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert tui._pending_terminal_operations == {}
    assert tui._committed_line_count == 0
    assert tui._visible_committed_rows == 0
    assert tui._scroll_epoch == original_epoch + 1
    assert tui._full_layout_invalidated is True
@pytest.mark.asyncio
async def test_worker_commit_token_failure_preserves_state_and_output_requests(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    dock.append_message("retry after token failure")
    dock.request_force_flush()
    dock.queue_guidance_echo("retry guidance")

    token = tui._flush_committed()
    assert token is writer.tokens[0]

    writer.tokens[0].future.set_exception(RuntimeError("commit write failed"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert tui._committed_line_count == 0
    assert tui._visible_committed_rows == 0
    assert dock.consume_force_flush_request() is True
    assert dock.consume_guidance_echoes() == ["retry guidance"]
    assert tui._render_state.pending_commit_tokens == []
    assert tui._render_state.pending_commit_updates == {}
    assert tui._render_state.pending_commit_tasks == {}



def _append_safe_durable_turn(text: str):
    turn = dock.start_turn(text)
    dock.append_message(f"answer {text}")
    dock.end_turn()
    dock.tree.mark_root_turn_durable(turn.payload["transcript_turn_id"])
    return turn


def test_live_history_retention_keeps_latest_twenty_committed_turns(tmp_path):
    tui = _tui(tmp_path)
    turns = [_append_safe_durable_turn(f"turn {index}") for index in range(21)]
    width = tui._frame_width()
    committed_lines = len(dock.tree.render(width))
    dock.tree.mark_root_turns_committed_through_line(width, committed_lines)
    tui._committed_line_count = committed_lines

    tui._apply_live_history_retention(width)

    retained_ids = [
        node.payload["transcript_turn_id"]
        for node in dock.tree.root.children
        if node.node_type == "turn"
    ]
    assert retained_ids == [turn.payload["transcript_turn_id"] for turn in turns[1:]]
    assert tui._last_evicted_turn_ids == [turns[0].payload["transcript_turn_id"]]
    assert tui._retained_root_turn_count == 20
    assert tui._committed_line_count == len(dock.tree.render(width))


def test_live_history_retention_uses_projected_body_byte_limit(tmp_path):
    tui = _tui(tmp_path)
    tui.LIVE_HISTORY_PROJECTED_BODY_LIMIT = 1
    first = _append_safe_durable_turn("oldest")
    second = _append_safe_durable_turn("latest")
    width = tui._frame_width()
    committed_lines = len(dock.tree.render(width))
    dock.tree.mark_root_turns_committed_through_line(width, committed_lines)
    tui._committed_line_count = committed_lines

    tui._apply_live_history_retention(width)

    retained_ids = [
        node.payload["transcript_turn_id"]
        for node in dock.tree.root.children
        if node.node_type == "turn"
    ]
    assert retained_ids == [second.payload["transcript_turn_id"]]
    assert tui._last_evicted_turn_ids == [first.payload["transcript_turn_id"]]
    assert tui._projected_body_bytes > tui.LIVE_HISTORY_PROJECTED_BODY_LIMIT


@pytest.mark.asyncio
async def test_live_history_eviction_waits_for_completed_writer_token(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui.LIVE_HISTORY_ROOT_TURN_LIMIT = 1
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    first = _append_safe_durable_turn("first")
    second = _append_safe_durable_turn("second")

    tui._flush_committed(force=True)

    assert [
        node.payload["transcript_turn_id"]
        for node in dock.tree.root.children
        if node.node_type == "turn"
    ] == [first.payload["transcript_turn_id"], second.payload["transcript_turn_id"]]

    writer.tokens[0].future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert [
        node.payload["transcript_turn_id"]
        for node in dock.tree.root.children
        if node.node_type == "turn"
    ] == [second.payload["transcript_turn_id"]]
    assert tui._last_evicted_turn_ids == [first.payload["transcript_turn_id"]]


@pytest.mark.asyncio
async def test_restored_history_enters_retention_after_live_commit(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    restored_dock = type(dock)()
    restored_dock.begin_capture()
    for index in range(21):
        turn = restored_dock.start_turn(f"restored {index}")
        restored_dock.append_message(f"answer {index}")
        restored_dock.end_turn()
        restored_dock.tree.mark_root_turn_durable(
            turn.payload["transcript_turn_id"]
        )
    dock.restore_tree(restored_dock.tree)

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    tui._sync_restored_render_state()
    tui._has_rendered_frame = True
    dock.start_turn("live")
    dock.append_message("live answer")
    dock.end_turn()
    live_turn = next(
        node for node in reversed(dock.tree.root.children)
        if node.node_type == "turn"
    )
    dock.tree.mark_root_turn_durable(live_turn.payload["transcript_turn_id"])

    tui._flush_committed(force=True)
    writer.tokens[0].future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    retained_ids = [
        node.payload["transcript_turn_id"]
        for node in dock.tree.root.children
        if node.node_type == "turn"
    ]
    assert retained_ids == list(range(2, 22))
    assert dock.restored_root_child_range() is None
    assert tui._retained_root_turn_count == 20


@pytest.mark.asyncio
async def test_drain_committed_output_flushes_tail_added_while_token_pending(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    writer.drain_async = lambda: asyncio.sleep(0)
    tui._terminal_writer = writer
    dock.append_message("first batch")

    tui._flush_committed(force=True)
    dock.append_message("second batch")
    dock.queue_guidance_echo("tail guidance")
    drain_task = asyncio.create_task(tui._drain_committed_output())

    writer.tokens[0].future.set_result(None)
    for _ in range(5):
        await asyncio.sleep(0)
        if len(writer.tokens) == 2:
            break
    assert len(writer.tokens) == 2
    writer.tokens[1].future.set_result(None)
    await drain_task

    assert "second batch" in writer.commits[1]["ansi"]
    assert "tail guidance" in writer.commits[1]["ansi"]
    assert tui._pending_commit_tokens == []
    assert tui._pending_commit_updates == {}
    assert tui._pending_commit_tasks == {}




@pytest.mark.asyncio
async def test_worker_commit_settles_only_immutable_submitted_snapshot(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer

    submitted = dock.append_message("submitted batch")
    tui._flush_committed(force=True)
    added_after_submit = dock.tree.new_node(
        parent=submitted,
        node_type="message",
        header="added after submit",
        collapsed=False,
        payload={"lifecycle": "completed"},
    )

    assert submitted.id not in dock._settled_node_ids
    assert added_after_submit.id not in dock._settled_node_ids

    writer.tokens[0].future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert submitted.id in dock._settled_node_ids
    assert added_after_submit.id not in dock._settled_node_ids




def _start_deferred_tool_result():
    dock.start_turn("read the file")
    tool = dock.start_tool(
        "Reading",
        'file_path="src/app.py"',
        tool_name="read",
        tool_call_id="read-deferred",
        raw_args={"file_path": "src/app.py"},
    )
    dock.finish_tool_node(tool, "Read", 0.1, True, "done")
    result = dock.append_tool_result(
        "first result",
        parent=tool,
        tool_call_id="read-deferred",
    )
    assert result is not None
    return tool, result


async def _resolve_deferred_commit(token, *, failed=False):
    if failed:
        token.future.set_exception(RuntimeError("terminal write failed"))
    else:
        token.future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_result_update_while_first_writer_batch_is_pending_keeps_result_in_activity(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    tool, result = _start_deferred_tool_result()

    tui._flush_committed(force=True)
    first_token = writer.tokens[0]
    assert "Read" in writer.commits[0]["ansi"]
    assert "first result" not in writer.commits[0]["ansi"]
    dock.append_tool_result(
        "second result",
        parent=tool,
        tool_call_id="read-deferred",
    )

    await _resolve_deferred_commit(first_token)

    assert tool.id in dock._settled_node_ids
    assert result.id not in dock._settled_node_ids
    tui._flush_committed(force=True)
    assert len(writer.tokens) == 1
    assert result.id not in dock._settled_node_ids

    with tui._console.capture() as capture:
        tui._console.print(tui._render_impl())
    assert "second result" in capture.get()


@pytest.mark.asyncio
async def test_result_update_after_first_writer_batch_stays_activity_only(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    tool, result = _start_deferred_tool_result()

    tui._flush_committed(force=True)
    await _resolve_deferred_commit(writer.tokens[0])
    assert tool.id in dock._settled_node_ids
    assert result.id not in dock._settled_node_ids

    dock.append_tool_result(
        "second result",
        parent=tool,
        tool_call_id="read-deferred",
    )
    tui._flush_committed(force=True)

    assert len(writer.tokens) == 1
    assert "second result" not in writer.commits[0]["ansi"]
    assert result.id not in dock._settled_node_ids
    with tui._console.capture() as capture:
        tui._console.print(tui._render_impl())
    assert "second result" in capture.get()


@pytest.mark.asyncio
async def test_result_update_after_failed_first_writer_batch_retries_call_only(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    tool, result = _start_deferred_tool_result()

    tui._flush_committed(force=True)
    first_token = writer.tokens[0]
    dock.append_tool_result(
        "second result",
        parent=tool,
        tool_call_id="read-deferred",
    )
    await _resolve_deferred_commit(first_token, failed=True)

    assert tool.id not in dock._settled_node_ids
    assert result.id not in dock._settled_node_ids
    tui._flush_committed(force=True)
    assert len(writer.tokens) == 2
    assert "Read" in writer.commits[1]["ansi"]
    assert "second result" not in writer.commits[1]["ansi"]

    await _resolve_deferred_commit(writer.tokens[1])
    assert tool.id in dock._settled_node_ids
    assert result.id not in dock._settled_node_ids


@pytest.mark.asyncio
async def test_multiple_result_updates_do_not_enqueue_additional_writer_batches(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer
    tool, result = _start_deferred_tool_result()

    tui._flush_committed(force=True)
    await _resolve_deferred_commit(writer.tokens[0])
    dock.append_tool_result(
        "second result",
        parent=tool,
        tool_call_id="read-deferred",
    )
    tui._flush_committed(force=True)
    dock.append_tool_result(
        "third result",
        parent=tool,
        tool_call_id="read-deferred",
    )
    tui._flush_committed(force=True)

    assert len(writer.tokens) == 1
    assert "third result" not in writer.commits[0]["ansi"]
    assert tool.id in dock._settled_node_ids
    assert result.id not in dock._settled_node_ids
    with tui._console.capture() as capture:
        tui._console.print(tui._render_impl())
    assert "third result" in capture.get()


@pytest.mark.asyncio
async def test_worker_commit_failure_does_not_settle_submitted_snapshot(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer

    submitted = dock.append_message("failed batch")
    tui._flush_committed(force=True)
    writer.tokens[0].future.set_exception(RuntimeError("terminal write failed"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert submitted.id not in dock._settled_node_ids


@pytest.mark.parametrize("tty", [False, True])
def test_synchronous_writer_settles_after_flush(tmp_path, monkeypatch, tty):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = tty
    if tty:
        tui._console = Console(
            file=None,
            force_terminal=True,
            width=80,
            height=24,
            _environ={},
        )

    submitted = dock.append_message("synchronous batch")
    tui._flush_committed(force=True)

    assert submitted.id in dock._settled_node_ids


def test_worker_without_wait_settles_after_submit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui, writer = _worker_commit_tui(tmp_path)
    submitted = dock.append_message("compatibility batch")

    tui._flush_committed(force=True)

    assert writer.commits
    assert submitted.id in dock._settled_node_ids

def test_invalidate_layout_clears_snapshots_and_rejects_old_callback(
    tmp_path, monkeypatch
):
    from voidx_cli.terminal_writer import FrameResult

    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    tui._render_frame()
    pending = tui._pending_layout_snapshots[1]
    tui._applied_layout_snapshot = pending
    original_epoch = tui._scroll_epoch

    tui._invalidate_layout("resize")

    assert tui._scroll_epoch == original_epoch + 1
    assert tui._applied_layout_snapshot is None
    assert tui._pending_layout_snapshots == {}
    assert tui._pending_layout_force_full == {}
    assert tui._full_layout_invalidated is True

    tui._handle_terminal_frame_result(FrameResult(1, 1, 1, 0.1, "full", True))
    assert tui._applied_layout_snapshot is None



def test_non_full_frame_result_does_not_clear_layout_invalidation(
    tmp_path, monkeypatch
):
    from voidx_cli.terminal_writer import FrameResult

    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    tui._render_frame()
    assert writer.frames[0].force_full is False
    tui._full_layout_invalidated = True

    tui._handle_terminal_frame_result(FrameResult(1, 1, 1, 0.1, "diff", True))

    assert tui._full_layout_invalidated is True



def test_sync_frame_registers_bounded_physical_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 6)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=6, _environ={})
    tui._input_lines = [f"input {index}" for index in range(10)]
    tui._cursor_row = 9
    tui._cursor_col = len(tui._input_lines[-1])

    tui._render_frame()

    assert tui._applied_layout_snapshot is not None
    assert tui._applied_layout_snapshot.frame_rows == tui._last_frame_rows
    assert tui._last_frame_start_row + tui._last_frame_rows - 1 <= 6
    assert 1 <= tui._applied_layout_snapshot.cursor_row <= 6



def test_sync_frame_patch_keeps_absolute_cursor_and_avoids_screen_erase(
    tmp_path, monkeypatch
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 20)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=20, _environ={})
    dock.begin_capture()
    dock.set_stream("first line")
    tui._render_frame()

    fake_stdout.text = ""
    dock.set_stream("first line\nsecond line")
    tui._render_frame()

    assert "\x1b[J" not in fake_stdout.text
    assert "\x1b[K" in fake_stdout.text
    assert re.search(r"\x1b\[\d+;\d+H", fake_stdout.text)
    assert tui._applied_layout_snapshot is not None



def test_sync_input_region_uses_snapshot_patch_without_screen_erase(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    dock.append_message("stable transcript")
    tui._render_frame()
    fake_stdout.text = ""

    tui._input_lines = ["changed input"]
    tui._cursor_col = len("changed input")
    tui._render_input_region()

    assert "\x1b[J" not in fake_stdout.text
    assert "changed input" in Text.from_ansi(fake_stdout.text).plain


def test_sync_input_region_without_snapshot_falls_back_to_full_frame(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    tui._has_rendered_frame = True
    tui._last_bottom_rows = 3
    calls = []
    monkeypatch.setattr(tui, "_render_frame", lambda: calls.append("frame"))

    tui._render_input_region()

    assert calls == ["frame"]




def test_sync_input_region_repaints_when_command_panel_appears(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 20)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=20, _environ={})
    dock.append_message("stable transcript")
    tui._render_frame()
    fake_stdout.text = ""

    tui._process_input(b"/")
    tui._render_after_input()

    output = Text.from_ansi(fake_stdout.text).plain
    assert tui._command_panel_active is True
    assert "/agents" in output


def test_sync_input_region_repaints_when_command_panel_filter_changes(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 20)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=20, _environ={})
    dock.append_message("stable transcript")
    tui._render_frame()

    tui._process_input(b"/")
    tui._render_after_input()
    fake_stdout.text = ""

    tui._process_input(b"age")
    tui._render_after_input()

    output = Text.from_ansi(fake_stdout.text).plain
    assert "/agents" in output
    assert "/allow" not in output


def test_sync_input_region_repaints_when_command_panel_disappears(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 20)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=20, _environ={})
    dock.append_message("stable transcript")
    tui._render_frame()

    tui._process_input(b"/")
    tui._render_after_input()
    fake_stdout.text = ""

    tui._process_input(b"\x7f")
    tui._render_after_input()

    output = Text.from_ansi(fake_stdout.text).plain
    assert tui._command_panel_active is False
    assert "/agents" not in output
    # Stale panel rows (below the 4-row bottom: transcript, separator, input, separator) must be erased.
    assert "\x1b[5;1H\x1b[K" in fake_stdout.text


def test_sync_input_region_keeps_local_patch_when_panel_unchanged(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 20)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=20, _environ={})
    dock.append_message("stable transcript")
    tui._render_frame()
    fake_stdout.text = ""

    fallback_calls = []
    monkeypatch.setattr(
        tui,
        "_render_sync_local_frame",
        lambda **kwargs: fallback_calls.append(kwargs) or True,
    )

    tui._process_input(b"a")
    tui._render_after_input()

    output = Text.from_ansi(fake_stdout.text).plain
    assert fallback_calls == []
    assert "a" in output


def test_sync_choice_selection_patch_only_writes_choice_region(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 20)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=20, _environ={})
    dock.append_message("stable transcript")
    tui._active_choice = [("one", "one", ""), ("two", "two", "")]
    tui._choice_prompt = "Pick?"
    tui._choice_selected = 0
    tui._render_frame()
    fake_stdout.text = ""

    tui._choice_selected = 1
    assert tui._render_choice_selection_region() is True

    output = Text.from_ansi(fake_stdout.text).plain
    assert "Pick?" in output
    assert "stable transcript" not in output
    assert "\x1b[J" not in fake_stdout.text
    assert "\x1b[K" in fake_stdout.text



def test_sync_busy_tick_requires_trusted_layout_cache(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    tui._busy = True
    tui._busy_started_at = 0.0
    tui._busy_activity_verb = "Working"
    tui._render_frame()
    fake_stdout.text = ""
    tui._invalidate_frame_cache()

    assert tui._render_busy_activity_tick() is False
    assert fake_stdout.text == ""


@pytest.mark.asyncio
async def test_worker_resize_barrier_stays_pending_until_token_completes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )

    class DeferredToken:
        def __init__(self):
            self.future = asyncio.get_running_loop().create_future()

    class DeferredBarrierWriter(_WorkerFrameWriter):
        def __init__(self):
            super().__init__()
            self.tokens = []

        def submit_barrier(self, **kwargs):
            token = DeferredToken()
            self.tokens.append(token)
            self.barriers.append(kwargs)
            self.events.append(("barrier", kwargs["kind"]))
            return token

        async def wait(self, token):
            await token.future

    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = DeferredBarrierWriter()
    tui._terminal_writer = writer
    tui._prev_frame_width = 60
    tui._prev_frame_term_height = 24

    tui._render_frame()

    token = writer.tokens[0]
    operation = tui._pending_terminal_operations[id(token)]
    assert operation["kind"] == "barrier"
    assert operation["token"] is token

    token.future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert tui._pending_terminal_operations == {}


@pytest.mark.asyncio
async def test_worker_make_room_registers_scroll_barrier_until_completion(
    tmp_path,
):
    class DeferredScrollWriter(_WorkerFrameWriter):
        def __init__(self):
            super().__init__()
            self.tokens = []

        def submit_barrier(self, **kwargs):
            token = _DeferredCommitToken(asyncio.get_running_loop())
            self.tokens.append(token)
            self.barriers.append(kwargs)
            self.events.append(("barrier", kwargs["kind"]))
            return token

        async def wait(self, token):
            await token.future

    tui = _tui(tmp_path)
    tui._tty = True
    writer = DeferredScrollWriter()
    tui._terminal_writer = writer
    tui._visible_committed_rows = 5

    assert tui._make_room_for_frame(frame_rows=8, term_height=10) is True

    token = writer.tokens[0]
    assert id(token) in tui._pending_terminal_operations
    assert tui._pending_terminal_operations[id(token)]["kind"] == "barrier"

    token.future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert tui._pending_terminal_operations == {}


class _DelayedBarrierToken:
    def __init__(self, loop):
        self._future = loop.create_future()


class _DelayedBarrierWriter:
    worker_mode = True

    def __init__(self, loop):
        self.loop = loop
        self.barriers = []
        self.frames = []
        self.tokens = []

    def submit_barrier(self, **kwargs):
        token = _DelayedBarrierToken(self.loop)
        self.barriers.append(kwargs)
        self.tokens.append(token)
        return token

    def submit_frame(self, batch):
        self.frames.append(batch)

    async def wait(self, token):
        await token._future


@pytest.mark.asyncio
async def test_worker_frame_defers_scroll_state_until_barrier_applies(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    writer = _DelayedBarrierWriter(asyncio.get_running_loop())
    tui._terminal_writer = writer
    tui._visible_committed_rows = 12

    tui._render_frame()

    assert writer.barriers
    assert writer.frames
    assert tui._visible_committed_rows == 12

    writer.tokens[0]._future.set_result(None)
    await asyncio.sleep(0)

    assert tui._visible_committed_rows < 12




@pytest.mark.asyncio
async def test_worker_clear_state_waits_for_barrier_before_applying(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=12, _environ={})
    writer = _DelayedBarrierWriter(asyncio.get_running_loop())
    tui._terminal_writer = writer
    projection = object()
    tui._committed_line_count = 7
    tui._committed_projection = projection
    tui._visible_committed_rows = 5
    dock.reset()

    tui._render_frame()

    assert writer.barriers[0]["kind"] == "clear"
    assert tui._committed_line_count == 7
    assert tui._committed_projection is projection
    assert tui._visible_committed_rows == 5

    writer.tokens[0]._future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert tui._committed_line_count == 0
    assert tui._committed_projection is None
    assert tui._visible_committed_rows == 0

class _FailingSyncFrameWriter:
    worker_mode = False

    def __init__(self):
        self.flush_calls = 0
        self.fail_flush = False
        self.values = []
        self.recovery_errors = []

    def write(self, value):
        self.values.append(value)
        return len(value)

    def flush(self):
        self.flush_calls += 1
        if self.fail_flush:
            raise OSError("frame flush failed")

    def _recover_sync_failure(self, error):
        self.recovery_errors.append(error)


def test_sync_frame_flush_failure_does_not_publish_renderer_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _FailingSyncFrameWriter()
    tui._terminal_writer = writer

    tui._render_frame()
    previous = (
        tui._applied_layout_snapshot,
        tui._prev_frame_lines,
        tui._terminal_frame_generation,
        tui._layout_generation,
        tui._last_frame_rows,
    )
    writer.fail_flush = True

    with pytest.raises(OSError, match="frame flush failed"):
        tui._render_frame()

    assert tui._applied_layout_snapshot is None
    assert tui._prev_frame_lines is None
    assert tui._terminal_frame_generation == previous[2]
    assert tui._layout_generation == previous[3]
    assert tui._last_frame_rows == previous[4]
    assert writer.recovery_errors
    assert tui._terminal_submission_failed is True


def test_sync_local_io_failure_does_not_recurse_into_full_frame(tmp_path, monkeypatch):
    from types import SimpleNamespace

    tui = _tui(tmp_path)
    tui._tty = True
    tui._terminal_writer = _FailingSyncFrameWriter()
    previous = SimpleNamespace(frame_start_row=1)
    tui._applied_layout_snapshot = previous
    tui._terminal_frame_generation = 3
    fallback_calls = []
    monkeypatch.setattr(tui, "_sync_snapshot_is_usable", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        tui,
        "_render_impl",
        lambda **kwargs: setattr(
            tui,
            "_render_plan",
            SimpleNamespace(logical_plan=object()),
        ),
    )
    monkeypatch.setattr(
        tui,
        "_physical_viewport_for_frame",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(tui, "_physical_target_lines", lambda physical: ["line"])
    monkeypatch.setattr(
        tui,
        "_layout_snapshot_for_physical",
        lambda **kwargs: SimpleNamespace(
            frame_start_row=1,
            frame_rows=1,
            cursor_row=1,
            cursor_col=1,
            generation=4,
        ),
    )
    monkeypatch.setattr(tui, "_snapshot_geometry_matches", lambda *args: True)
    monkeypatch.setattr(
        tui,
        "_apply_sync_snapshot_patch",
        lambda **kwargs: (_ for _ in ()).throw(OSError("local write failed")),
    )
    monkeypatch.setattr(tui, "_render_frame", lambda: fallback_calls.append(True))

    assert tui._render_sync_local_frame() is False
    assert fallback_calls == []
    assert tui._terminal_submission_failed is True


@pytest.mark.parametrize("startup_flushed", [False, True])
def test_restored_logical_transcript_is_complete_before_viewport_projection(
    tmp_path, monkeypatch, startup_flushed
):
    monkeypatch.setattr(sys, "stdout", _FakeStdout())
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(force_terminal=True, width=80, height=24, _environ={})
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="startup",
        header="STARTUP-PREFIX",
    )
    restored = type(dock.tree)()
    for index in range(260):
        restored.new_node(
            parent=restored.root,
            node_type="message",
            header=f"RESTORED-{index:03d}",
            payload={"index": index},
        )
    dock.restore_tree(restored, append=True)
    if startup_flushed:
        tui._flush_committed(force=True)
        assert tui._restored_startup_flushed is True
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="NEW-AFTER-RESTORE",
    )
    nodes_before = tuple(dock.tree.root.children)
    payloads_before = [dict(node.payload) for node in nodes_before]
    source_rows = []

    for height in (6, 24, 60):
        tui._render_impl(height=height, capture_plan=True)
        logical = tui._render_plan.logical_plan
        transcript = logical.source_regions[0]
        plain = Text.from_ansi(transcript.ansi).plain
        assert all(f"RESTORED-{index:03d}" in plain for index in range(260))
        assert "NEW-AFTER-RESTORE" in plain
        assert ("STARTUP-PREFIX" in plain) is not startup_flushed
        source_rows.append(transcript.rows)
        physical = tui._physical_viewport_for_frame(
            logical,
            width=tui._frame_width(),
            term_height=height,
            frame_start_row=1,
        )
        assert len(tui._physical_target_lines(physical)) <= height
        assert physical.bottom.region.start_row <= physical.cursor_row <= height

    assert source_rows[0] == source_rows[1] == source_rows[2]
    assert tuple(dock.tree.root.children) == nodes_before
    assert [node.payload for node in nodes_before] == payloads_before
    assert dock.restored_root_child_range() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["scroll", "resize", "clear"])
async def test_completed_barrier_waiter_cannot_erase_newly_applied_frame(
    tmp_path, monkeypatch, boundary
):
    from voidx_cli.terminal_writer import FrameResult

    monkeypatch.setattr(
        shutil, "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 12)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(force_terminal=True, width=80, height=12, _environ={})
    writer = _DelayedBarrierWriter(asyncio.get_running_loop())
    tui._terminal_writer = writer
    if boundary == "scroll":
        tui._visible_committed_rows = 12
    elif boundary == "resize":
        tui._prev_frame_width = 60
    else:
        dock.request_clear_screen()
    applied = []
    apply = getattr(tui, f"_apply_{boundary}_state")

    def record_apply(*args):
        applied.append(boundary)
        apply(*args)

    monkeypatch.setattr(tui, f"_apply_{boundary}_state", record_apply)
    tui._render_frame()
    tasks = tuple(op["task"] for op in tui._pending_terminal_operations.values())
    await asyncio.sleep(0)
    assert applied == []
    for token in writer.tokens:
        token._future.set_result(None)
    batch = writer.frames[-1]
    tui._handle_terminal_frame_result(FrameResult(
        generation=batch.generation,
        total_lines=len(batch.target_lines),
        changed_lines=len(batch.target_lines),
        render_ms=0,
        strategy="full",
        applied=True,
    ))
    snapshot = tui._applied_layout_snapshot
    await asyncio.gather(*tasks)

    assert applied == [boundary]
    assert tui._applied_layout_snapshot is snapshot
    assert tui._prev_frame_lines == list(batch.target_lines)
    assert tui._last_render_plan is not None
    assert tui._pending_terminal_operations == {}


class _RequiredWorkerFrameWriter(_WorkerFrameWriter):
    worker_mode = False


def test_worker_owned_tty_frame_never_falls_back_to_sync_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._terminal_writer_required = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _RequiredWorkerFrameWriter()
    tui._terminal_writer = writer

    tui._render_frame()

    assert len(writer.frames) == 1


class _RequiredWorkerCommitWriter(_WorkerCommitWriter):
    worker_mode = False


def test_worker_owned_tty_commit_never_falls_back_to_sync_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._terminal_writer_required = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _RequiredWorkerCommitWriter()
    tui._terminal_writer = writer
    tui._has_rendered_frame = True
    tui._last_frame_start_row = 6
    tui._prev_frame_lines = ["active frame"]
    tui._prev_frame_width = 79
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="worker-owned commit",
        collapsed=False,
    )

    tui._flush_committed(force=True)

    assert len(writer.commits) == 1


def test_worker_owned_tty_scroll_never_falls_back_to_sync_writer(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._terminal_writer_required = True
    writer = _RequiredWorkerFrameWriter()
    tui._terminal_writer = writer
    tui._visible_committed_rows = 5

    assert tui._make_room_for_frame(frame_rows=8, term_height=10) is True

    assert writer.barriers == [
        {
            "kind": "scroll",
            "ansi": "\x1b[10;1H" + "\n" * 3,
        }
    ]
    assert tui._visible_committed_rows == 2


@pytest.mark.asyncio
async def test_consecutive_stream_in_turn_does_not_duplicate_assistant_header_in_tui(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitFrameWriter()
    tui._terminal_writer = writer

    dock.begin_capture()
    dock.start_turn("test question")
    tui._flush_committed()

    draft_text = "• 已完成长对话 CPU 100% 性能瓶颈的测试用例编写、根因修复与全量验证。\n### 一、根本原因分析与复现"
    dock.set_stream(draft_text, refresh=False)
    dock.commit_stream(refresh=False)
    tui._flush_committed()
    tui._render_frame()

    extended_text = (
        "• 已完成长对话 CPU 100% 性能瓶颈的测试用例编写、根因修复与全量验证。\n"
        "### 一、根本原因分析与复现\n"
        "通过对长会话中占满 100% CPU 的后台进程采样分析，发现以下三个瓶颈叠\n"
        "1. OutputNode 数据类深度值比较（主要热点）\n"
        "2. mark_root_turns_committed_through_line O(N^2) 累计渲染"
    )
    dock.set_stream(extended_text, refresh=False)
    dock.commit_stream(refresh=False)
    tui._flush_committed()
    tui._render_frame()

    lines = [line for line in dock.tree.render(80) if "已完成长对话" in line]
    assert len(lines) == 1

    agent = dock.ensure_agent()
    assistant_children = [child for child in agent.children if child.node_type == "assistant"]
    assert len(assistant_children) == 1


@pytest.mark.parametrize(
    ("tool_name", "label", "args", "raw_args", "call_text", "result_text"),
    [
        (
            "read",
            "Reading",
            'file_path="src/file.py"',
            {"file_path": "src/file.py"},
            "Read",
            "READ_RESULT_MUST_STAY_IN_ACTIVITY",
        ),
        (
            "bash",
            "Running",
            'command="printf result"',
            {"command": "printf result"},
            "Bash",
            "BASH_RESULT_MUST_STAY_IN_ACTIVITY",
        ),
    ],
)
def test_tool_result_stays_in_activity_area_and_tool_call_reaches_scrollback(
    tmp_path,
    monkeypatch,
    tool_name,
    label,
    args,
    raw_args,
    call_text,
    result_text,
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(file=None, force_terminal=False, width=80, height=24, _environ={})
    dock.begin_capture()
    dock.start_turn("run a tool")
    tool = dock.start_tool(
        label,
        args,
        tool_name=tool_name,
        tool_call_id=f"{tool_name}-scrollback",
        raw_args=raw_args,
    )
    dock.finish_tool_node(tool, call_text, 0.1, True, "done")
    dock.append_tool_result(
        result_text,
        parent=tool,
        tool_call_id=f"{tool_name}-scrollback",
    )

    tui._flush_committed(force=True)

    assert call_text in fake_stdout.text
    assert result_text not in fake_stdout.text



def test_restored_tool_result_remains_in_activity_after_full_watermark(
    tmp_path,
    monkeypatch,
):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(file=None, force_terminal=False, width=80, height=12, _environ={})

    restored = type(dock.tree)()
    restored.new_node(
        parent=restored.root,
        node_type="message",
        header="restored history",
        collapsed=False,
    )
    dock.restore_tree(restored)
    result = dock.tree.new_node(
        parent=dock.tree.root,
        node_type="tool_result",
        header="RESTORED_RESULT_MUST_STAY_IN_ACTIVITY",
        collapsed=False,
        status="done",
        payload={"lifecycle": "completed"},
    )

    tui._flush_committed(force=True)

    assert result.id not in dock._settled_node_ids
    assert dock.restored_root_child_range() == (0, 1)
    assert tui._restored_committed_line_count > 0
    assert "RESTORED_RESULT_MUST_STAY_IN_ACTIVITY" not in fake_stdout.text

    with tui._console.capture() as capture:
        tui._console.print(tui._render_impl())
    assert "RESTORED_RESULT_MUST_STAY_IN_ACTIVITY" in capture.get()



def test_resume_restored_history_is_completed_and_inactive(tmp_path):
    restored = type(dock.tree)()
    turn = restored.new_node(
        parent=restored.root,
        node_type="turn",
        header="[bold white]❯[/] restored question",
        collapsed=False,
        payload={
            "transcript_turn_id": 1,
            "lifecycle": "running",
            "active": True,
            "terminal": False,
            "committed": False,
            "durable": True,
            "render_pending": True,
        },
    )
    agent = restored.new_node(
        parent=turn,
        node_type="assistant",
        header="",
        collapsed=False,
        payload={"lifecycle": "running", "active": True, "terminal": False},
    )
    restored.new_node(
        parent=agent,
        node_type="assistant",
        header="● restored answer",
        collapsed=False,
        payload={
            "lifecycle": "running",
            "active": True,
            "terminal": False,
            "stream": True,
        },
    )

    dock.reset()
    dock.restore_tree(restored, append=True)

    nodes = []
    stack = list(dock.tree.root.children)
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(node.children)

    assert nodes
    assert all(node.payload.get("lifecycle") == "completed" for node in nodes)
    assert all(node.payload.get("active") is False for node in nodes)
    assert all(node.payload.get("terminal") is True for node in nodes)
    assert all(node.payload.get("render_pending") is False for node in nodes)
    assert all(node.payload.get("stream") is not True for node in nodes)


@pytest.mark.asyncio
async def test_prepare_session_switch_invalidates_pending_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitWriter()
    tui._terminal_writer = writer

    dock.reset()
    dock.begin_capture()
    dock.start_turn("old turn")
    tool = dock.start_tool("OldTool", 'arg="val"', tool_name="tool", tool_call_id="call-old")
    dock.finish_tool_node(tool, "OldTool", 0.1, True)
    diff = _append_previewed_file_diff(dock, tool, "diff body", path="old.py")

    token = tui._flush_committed(force=True)
    assert token is not None
    assert len(tui._render_state.pending_commit_tasks) == 1

    old_epoch = getattr(tui, "_restore_epoch", 0)
    await tui.prepare_session_switch()

    assert getattr(tui, "_restore_epoch", 0) == old_epoch + 1
    assert tui._visible_committed_rows == 0
    assert tui._committed_line_count == 0

    # Reset dock for the new session
    dock.reset()
    dock.begin_capture()
    dock.start_turn("new turn")

    # Complete the old deferred commit token
    writer.tokens[0].future.set_result(None)
    await asyncio.sleep(0.01)

    # Verify old commit does not apply state or settle nodes on the new tree
    assert tui._visible_committed_rows == 0
    assert tui._committed_line_count == 0
    assert not any(n.payload.get("settled") for n in dock.tree.root.children)


@pytest.mark.asyncio
async def test_handle_terminal_frame_result_drops_stale_restore_epoch(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    tui._restore_epoch = 1
    lines = ["line1", "line2"]
    snapshot = tui._layout_snapshot_for_frame(
        generation=1,
        width=80,
        term_height=24,
        start_row=1,
        lines=lines,
        frame_rows=2,
        bottom_rows=1,
        lines_up=0,
        cursor_ansi="",
    )
    assert snapshot is not None
    tui._pending_layout_snapshots[1] = snapshot

    # Advance restore epoch
    tui._restore_epoch = 2

    # Simulate frame result arrival from epoch 1
    from voidx_cli.terminal_writer import FrameResult
    result = FrameResult(
        generation=1,
        total_lines=2,
        changed_lines=2,
        render_ms=1.0,
        strategy="full",
        applied=True,
    )
    tui._handle_terminal_frame_result(result)

    # Should be dropped and not applied
    assert tui._applied_layout_snapshot is None

@pytest.mark.asyncio
async def test_worker_mode_busy_activity_tick_does_not_recollect_bottom(tmp_path, monkeypatch):
    from voidx_cli.terminal_writer import TerminalWriter

    class _CaptureStream:
        def __init__(self) -> None:
            self.value = ""

        def write(self, text: str) -> int:
            self.value += text
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    stream = _CaptureStream()
    writer = TerminalWriter(stream)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=tui._handle_terminal_frame_result,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    tui._terminal_writer = writer
    tui._terminal_writer_required = True

    try:
        tui._busy = True
        tui._busy_activity_verb = "Sublimating"
        tui._busy_activity_tick = 0

        tui._render_frame()
        await asyncio.wait_for(writer.drain_async(), timeout=1)

        bottom_calls = 0
        original_bottom = tui._render_bottom_elements
        def counting_render_bottom(*args, **kwargs):
            nonlocal bottom_calls
            bottom_calls += 1
            return original_bottom(*args, **kwargs)

        monkeypatch.setattr(tui, "_render_bottom_elements", counting_render_bottom)

        stream_len_before_tick = len(stream.value)
        tui._busy_activity_tick = 1
        ticked = tui._render_busy_activity_tick()
        assert ticked is True
        await asyncio.wait_for(writer.drain_async(), timeout=1)

        assert bottom_calls == 0
        tick_output = stream.value[stream_len_before_tick:]
        assert "\x1b[J" not in tick_output
        assert tui._render_stats.strategy == "diff"
        assert tui._render_stats.changed_lines == 1
    finally:
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)

@pytest.mark.asyncio
async def test_worker_mode_input_region_does_not_full_repaint_status(tmp_path, monkeypatch):
    from voidx_cli.terminal_writer import TerminalWriter

    class _CaptureStream:
        def __init__(self) -> None:
            self.value = ""

        def write(self, text: str) -> int:
            self.value += text
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    stream = _CaptureStream()
    writer = TerminalWriter(stream)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=tui._handle_terminal_frame_result,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    tui._terminal_writer = writer
    tui._terminal_writer_required = True

    try:
        tui._render_frame()
        await asyncio.wait_for(writer.drain_async(), timeout=1)

        status_calls = 0
        original_status = tui._render_hint_lines
        def counting_render_status(*args, **kwargs):
            nonlocal status_calls
            status_calls += 1
            return original_status(*args, **kwargs)

        monkeypatch.setattr(tui, "_render_hint_lines", counting_render_status)

        stream_len_before_input = len(stream.value)
        assert tui._process_input(b"a") is True
        tui._render_after_input()
        await asyncio.wait_for(writer.drain_async(), timeout=1)

        assert status_calls == 0
        input_output = stream.value[stream_len_before_input:]
        assert "\x1b[J" not in input_output
        assert tui._render_stats.strategy == "diff"
        assert tui._render_stats.changed_lines == 1
    finally:
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)

@pytest.mark.asyncio
async def test_flush_committed_preserves_baseline_and_does_not_clear_bottom(tmp_path, monkeypatch):
    from voidx_cli.terminal_writer import TerminalWriter

    class _CaptureStream:
        def __init__(self) -> None:
            self.value = ""

        def write(self, text: str) -> int:
            self.value += text
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )

    tui = _tui(tmp_path)
    tui._tty = True
    tui._running = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    stream = _CaptureStream()
    writer = TerminalWriter(stream)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=tui._handle_terminal_frame_result,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    tui._terminal_writer = writer
    tui._terminal_writer_required = True

    try:
        dock.begin_capture()
        dock.start_turn("initial turn")
        dock.append_message("message 1")
        commit1 = tui._flush_committed()
        if commit1 is not None:
            task1 = tui._render_state.pending_commit_tasks.get(id(commit1))
            if task1 is not None:
                await task1
            else:
                await asyncio.wait_for(writer.wait(commit1), timeout=1)
        await asyncio.sleep(0)

        tui._render_frame()
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        await asyncio.sleep(0)

        dock.append_message("message 2")
        stream_len_before_commit = len(stream.value)
        commit_token = tui._flush_committed()
        assert commit_token is not None
        pending_task = tui._render_state.pending_commit_tasks.get(id(commit_token))
        if pending_task is not None:
            await pending_task
        else:
            await asyncio.wait_for(writer.wait(commit_token), timeout=1)
        await asyncio.sleep(0)

        commit_output = stream.value[stream_len_before_commit:]
        assert "\x1b[J" not in commit_output

        stream_len_before_frame2 = len(stream.value)
        tui._render_frame()
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        await asyncio.sleep(0)

        frame2_output = stream.value[stream_len_before_frame2:]
        assert "\x1b[J" not in frame2_output
        assert tui._render_stats is not None
        assert tui._render_stats.strategy == "diff"
    finally:
        tui._render_scheduled = False
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


def test_thinking_stream_elements_falls_back_to_integration_startup(tmp_path):
    tui = _tui(tmp_path)
    dock.clear_integration_startup()
    assert tui._active_thinking_stream_elements(80) == []

    from voidx.agent.domain.ui_events import IntegrationStartupItem
    dock.set_integration_startup_items([
        IntegrationStartupItem(
            category="mcp",
            key="mcp:test",
            label="test",
            status="connecting",
        )
    ])
    try:
        elements = tui._active_thinking_stream_elements(80)
        assert len(elements) >= 1
        assert any("test" in e.plain for e in elements)
    finally:
        dock.clear_integration_startup()
    assert tui._active_thinking_stream_elements(80) == []


@pytest.mark.asyncio
async def test_worker_pending_commit_invalidates_layout_snapshot(tmp_path, monkeypatch):
    from voidx_cli.terminal_writer import FrameResult

    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitFrameWriter()
    tui._terminal_writer = writer
    dock.begin_capture()
    dock.append_message("committed history")

    tui._render_frame()
    assert len(writer.frames) == 1
    batch = writer.frames[0]
    tui._handle_terminal_frame_result(
        FrameResult(batch.generation, len(batch.target_lines), 0, 1.0, "full", True)
    )
    assert tui._applied_layout_snapshot is not None

    token = tui._flush_committed()
    assert token is writer.tokens[0]
    assert writer.commits[0]["preserve_baseline"] is True

    # The pending commit shifts the frame start row on the worker; the
    # pre-commit layout snapshot must not survive, or sync-local repaint
    # paths would patch stale absolute rows.
    assert tui._applied_layout_snapshot is None

    await _resolve_deferred_commit(token)


@pytest.mark.asyncio
async def test_worker_frame_result_after_commit_does_not_promote_stale_snapshot(
    tmp_path, monkeypatch
):
    from voidx_cli.terminal_writer import FrameResult

    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitFrameWriter()
    tui._terminal_writer = writer
    dock.begin_capture()
    dock.append_message("committed history")

    tui._render_frame()
    assert len(writer.frames) == 1
    generation = writer.frames[0].generation

    token = tui._flush_committed()
    assert token is writer.tokens[0]

    # A frame result queued before the commit must not promote the stale
    # pre-commit snapshot while the commit is still pending.
    tui._handle_terminal_frame_result(
        FrameResult(generation, 1, 1, 1.0, "diff", True)
    )
    assert tui._applied_layout_snapshot is None

    await _resolve_deferred_commit(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["input", "choice", "vibe"])
async def test_worker_pending_commit_blocks_local_repaints(
    tmp_path, monkeypatch, trigger
):
    from voidx_cli.terminal_writer import FrameResult

    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitFrameWriter()
    tui._terminal_writer = writer
    dock.begin_capture()
    dock.append_message("initial history")

    first_commit = tui._flush_committed()
    assert first_commit is writer.tokens[0]
    await _resolve_deferred_commit(first_commit)

    tui._render_frame()
    assert len(writer.frames) == 1
    first_frame = writer.frames[0]
    tui._handle_terminal_frame_result(
        FrameResult(
            first_frame.generation,
            len(first_frame.target_lines),
            len(first_frame.target_lines),
            1.0,
            "full",
            True,
        )
    )
    assert tui._applied_layout_snapshot is not None

    dock.append_message("pending history")
    pending_commit = tui._flush_committed()
    assert pending_commit is writer.tokens[1]
    assert writer.commits[1]["preserve_baseline"] is True
    assert tui._applied_layout_snapshot is None

    frame_count = len(writer.frames)
    if trigger == "input":
        assert tui._process_input(b"a") is True
        tui._render_after_input()
    elif trigger == "choice":
        tui._active_choice = [("one", "one", "first")]
        assert tui._render_choice_selection_region() is True
    else:
        tui._busy = True
        tui._busy_activity_verb = "Sublimating"
        tui._busy_activity_tick = 1
        tui._render_scheduled = False
        monkeypatch.setattr(tui, "_busy_activity_tick_active", lambda: True)
        assert tui._render_busy_activity_tick() is True

    assert len(writer.frames) == frame_count

    await _resolve_deferred_commit(pending_commit)
    tui._render_frame()
    assert len(writer.frames) == frame_count + 1
    tui._running = False


@pytest.mark.asyncio
async def test_worker_commit_snapshot_invalidation_preserves_baseline_state(
    tmp_path, monkeypatch
):
    from voidx_cli.terminal_writer import FrameResult

    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 24)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    writer = _DeferredCommitFrameWriter()
    tui._terminal_writer = writer
    dock.begin_capture()
    dock.append_message("initial history")

    first_commit = tui._flush_committed()
    await _resolve_deferred_commit(first_commit)
    tui._render_frame()
    first_frame = writer.frames[0]
    tui._handle_terminal_frame_result(
        FrameResult(
            first_frame.generation,
            len(first_frame.target_lines),
            len(first_frame.target_lines),
            1.0,
            "full",
            True,
        )
    )
    previous_baseline = (
        tui._prev_frame_lines,
        tui._prev_frame_start_row,
        tui._prev_frame_width,
        tui._prev_frame_term_height,
    )
    previous_epoch = tui._scroll_epoch

    dock.append_message("next history")
    pending_commit = tui._flush_committed()

    assert (
        tui._prev_frame_lines,
        tui._prev_frame_start_row,
        tui._prev_frame_width,
        tui._prev_frame_term_height,
    ) == previous_baseline
    assert tui._scroll_epoch == previous_epoch + 1
    assert tui._full_layout_invalidated is False

    await _resolve_deferred_commit(pending_commit)
    tui._running = False
