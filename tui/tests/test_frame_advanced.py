from tui_helpers import *  # noqa: F403

import os
import re
import shutil
import sys

import pytest

from rich.console import Console
from rich.text import Text

from voidx_cli.helpers import _rendered_row_count
from voidx.presentation.output.dock import dock


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

    calls = {"status": 0, "panel": 0, "thinking": 0, "bottom": 0}

    def render_status():
        calls["status"] += 1
        return [Text("status")]

    def render_panel(width):
        calls["panel"] += 1
        return ["[bold]panel[/bold]"]

    def render_thinking(width):
        calls["thinking"] += 1
        return [Text("thinking")]

    original_bottom = tui._render_bottom_elements

    def render_bottom(*args, **kwargs):
        calls["bottom"] += 1
        return original_bottom(*args, **kwargs)

    monkeypatch.setattr(tui, "_render_hint_lines", render_status)
    monkeypatch.setattr(tui, "_render_panel_lines", render_panel)
    monkeypatch.setattr(tui, "_active_thinking_stream_elements", render_thinking)
    monkeypatch.setattr(tui, "_render_bottom_elements", render_bottom)

    tui._render_frame()

    assert calls == {"status": 1, "panel": 1, "thinking": 1, "bottom": 1}

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



def test_render_impl_uses_tree_tail_for_restored_long_history(tmp_path, monkeypatch):
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
    assert rendered.index("new user") < rendered.index("new ai message")
    assert rendered.rfind("history C") < rendered.index("new user")
    assert rendered.count("history C") == 1




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
    assert batch.cursor_ansi.startswith("\x1b[")
    assert batch.cursor_ansi.endswith("G")
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


def test_worker_frame_generation_advances_only_after_successful_submit(
    tmp_path, monkeypatch
):
    tui, writer = _worker_render_tui(tmp_path, monkeypatch)
    writer.frame_error = RuntimeError("enqueue failed")

    with pytest.raises(RuntimeError, match="enqueue failed"):
        tui._render_frame()

    assert tui._terminal_frame_generation == 0
    assert writer.frames == []


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
    tui._visible_committed_rows = 5
    dock.reset()
    writer.barrier_error = RuntimeError("clear failed")

    with pytest.raises(RuntimeError, match="clear failed"):
        tui._render_frame()

    assert tui._committed_line_count == 7
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
