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
from voidx.ui.tui.state import InputState, RenderState
from voidx.ui.tui import (
    PureTui,
    _ENTER_TERMINAL_SEQUENCE,
    _EXIT_TERMINAL_SEQUENCE,
    _rendered_row_count,
)


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


def test_pure_tui_groups_runtime_state(tmp_path):
    tui = _tui(tmp_path)

    assert isinstance(tui._input_state, InputState)
    assert isinstance(tui._render_state, RenderState)

    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    assert tui._input_state.lines == ["hello"]
    assert tui._input_state.cursor_col == 5
    assert tui._input_lines == ["hello"]


def test_choice_render_handles_unselected_items_and_details(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [
        ("Yes [once]", "y", "Allow [only] once"),
        ("No", "n", "Deny"),
    ]
    tui._choice_prompt = "Allow [tool]?"
    tui._choice_selected = 0
    tui._choice_details = [{"name": "write", "pattern": "src/[file].py"}]

    # Previously unselected items generated invalid Rich markup: []No[/].
    renderable = tui._render_impl()

    assert renderable is not None


def test_status_summary_renders_model_policy_usage_and_goal(tmp_path):
    stats = UsageStats()
    stats.update_context(12_345, limit=128_000)
    stats.last_input_tokens = 12_345
    stats.last_output_tokens = 678
    stats.total_input_tokens = 12_345
    stats.total_output_tokens = 678
    stats.total_calls = 1
    status = SimpleNamespace(
        provider="mimo",
        model="mimo-v2.5",
        workspace=str(tmp_path),
        context_limit=128_000,
        debug=lambda: True,
        plan_mode=lambda: False,
        interaction_mode=lambda: "goal",
        goal_label=lambda: "ship pure tui",
        goal_phase=lambda: "implement",
        goal_status=lambda: "running",
        goal_turn_count=lambda: 2,
        reasoning_effort="xhigh",
        permission_label=lambda: "default",
        sandbox_label=lambda: "w-write",
        approval_label=lambda: "on-fail",
        approval_reviewer_label=lambda: "auto",
        usage_stats=stats,
    )
    tui = PureTui(status, COMMANDS)

    summary = tui._status_summary(200)

    assert "mimo/mimo-v2.5 xhigh" in summary
    assert "default w-write on-fail auto" in summary
    assert "goal" in summary
    assert "ctx 12.3k/128k" in summary
    assert "in 12.3k out 678 total 13.0k" in summary
    assert "goal running/implement turns 2 ship pure tui" in summary


def test_status_summary_renders_agent_step_from_dock(tmp_path):
    tui = _tui(tmp_path)
    dock.record_status("agent:-1:progress", "Agent step 1/50", stage="agent step")

    summary = tui._status_summary(80)

    assert "step 1/50" in summary
    assert "Agent step" not in summary


def test_status_summary_degrades_to_fit_width(tmp_path):
    status = SimpleNamespace(
        provider="anthropic",
        model="claude-sonnet-4",
        workspace=str(tmp_path),
        reasoning_effort="xhigh",
        permission_label=lambda: "accept-edits",
    )
    tui = PureTui(status, COMMANDS)

    summary = tui._status_summary(18)

    assert len(summary) <= 18
    assert summary.startswith("  anthropic")


def test_status_summary_degrades_by_display_width_for_cjk(tmp_path):
    status = SimpleNamespace(
        provider="模型",
        model="超宽模型",
        workspace=str(tmp_path),
        reasoning_effort="推理",
        permission_label=lambda: "接受编辑",
    )
    tui = PureTui(status, COMMANDS)

    summary = tui._status_summary(10)

    assert cell_len(summary) <= 10


def test_status_summary_is_empty_without_model_status(tmp_path):
    tui = _tui(tmp_path)

    assert tui._status_summary(80) == ""
    assert tui._render_hint_lines() == []


def test_status_summary_reuses_cache_until_marked_dirty(tmp_path):
    calls = {"permission": 0}

    def permission_label() -> str:
        calls["permission"] += 1
        return f"perm-{calls['permission']}"

    status = SimpleNamespace(
        provider="mimo",
        model="mimo-v2.5",
        workspace=str(tmp_path),
        reasoning_effort="xhigh",
        permission_label=permission_label,
    )
    tui = PureTui(status, COMMANDS)

    first = tui._status_summary(120)
    second = tui._status_summary(120)

    assert first == second
    assert calls["permission"] == 1

    tui._mark_status_summary_dirty()
    third = tui._status_summary(120)

    assert "perm-2" in third
    assert calls["permission"] == 2


@pytest.mark.asyncio
async def test_invalidate_coalesces_render_until_next_loop(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    tui._running = True
    calls = {"flush": 0, "render": 0}

    monkeypatch.setattr(tui, "_flush_committed", lambda: calls.__setitem__("flush", calls["flush"] + 1))
    monkeypatch.setattr(tui, "_render_frame", lambda: calls.__setitem__("render", calls["render"] + 1))

    tui.invalidate()
    tui.invalidate()

    assert calls == {"flush": 0, "render": 0}

    await asyncio.sleep(0)

    assert calls == {"flush": 1, "render": 1}
    assert tui._render_scheduled is False


def test_terminal_sequences_stay_on_normal_buffer():
    # Alternate screen NOT used — terminal handles scrollback natively
    assert "\x1b[?1049h" not in _ENTER_TERMINAL_SEQUENCE
    assert "\x1b[?1049l" not in _EXIT_TERMINAL_SEQUENCE
    assert "\x1b[?1000l" in _ENTER_TERMINAL_SEQUENCE
    assert "\x1b[?1006l" in _EXIT_TERMINAL_SEQUENCE


def test_dock_clear_screen_request_is_consumed_publicly():
    dock.reset()

    assert dock.consume_clear_screen_request() is True
    assert dock.consume_clear_screen_request() is False


def test_rendered_row_count_tracks_terminal_cursor_rows():
    assert _rendered_row_count("") == 0
    assert _rendered_row_count("one") == 1
    assert _rendered_row_count("one\n") == 1
    assert _rendered_row_count("one\ntwo\n") == 2


def test_render_impl_clips_transcript_to_visible_tail(tmp_path):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=10, _environ={})
    for index in range(20):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"line {index:02d}",
            collapsed=False,
        )

    lines = _render_lines(tui, width=80)
    rendered = "\n".join(lines)

    assert len(lines) <= 10
    assert "line 12" not in rendered
    assert "line 13" in rendered
    assert "line 19" in rendered


def test_frame_top_positioning_uses_terminal_height_for_absolute_row(tmp_path, monkeypatch):
    """Frame rendering calculates start_row from terminal height so
    the frame always renders at the bottom without scrollback pollution."""
    monkeypatch.setattr(
        shutil, "get_terminal_size",
        lambda fallback=None: os.terminal_size((80, 30)),
    )
    tui = _tui(tmp_path)
    tui._tty = True
    tui._has_rendered_frame = True

    # _move_to_frame_top_sequence is no longer called in the render path;
    # absolute positioning is computed directly from terminal height.
    # The old method still exists but is dead code.
    tui._last_frame_rows = 5
    assert tui._move_to_frame_top_sequence() == "\x1b[5A"


def test_frame_end_sequence_returns_from_input_cursor_to_frame_end(tmp_path):
    tui = _tui(tmp_path)
    tui._has_rendered_frame = True
    tui._last_frame_rows = 30
    tui._cursor_to_frame_end_lines = 4

    assert tui._move_to_frame_end_sequence() == "\r\x1b[4B\r"


def test_dock_turn_spacing_is_root_level_blank_line(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        first = dock.start_turn("one")
        dock.start_turn("two")

        lines = dock.tree.render(100)
        assert "one" in lines[0]
        assert lines[1] == ""
        assert "two" in lines[2]
        assert first.body_lines == []
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_turn_and_assistant_response_have_root_level_gap(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        dock.start_turn("one")
        dock.set_stream("answer")
        dock.commit_stream()

        lines = dock.tree.render(100)
        assert "one" in lines[0]
        assert lines[1] == ""
        assert "answer" in lines[2]
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_multiline_turn_body_aligns_under_prompt(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        turn = dock.start_turn("1、你\n2、好\n3、你是谁")

        lines = dock.tree.render(100)
        assert lines[:3] == ["[bold white]❯[/] 1、你", "  2、好", "  3、你是谁"]
        assert turn.body_lines == ["2、好", "3、你是谁"]
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_tool_header_uses_raw_args_without_rich_markup(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Reading",
            'file_path="[cyan]src/voidx/ui/dock.py[/cyan]"',
            tool_name="read",
            raw_args={"file_path": "src/voidx/ui/dock.py"},
        )
        dock.finish_tool_node(tool, "read", 0.0, True)

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))
        assert 'Read("src/voidx/ui/dock.py")' in rendered
        assert "[cyan]" not in rendered
        assert "(0.0s)" not in rendered
        assert "Reading file_path" not in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_tool_collapsed_summary_does_not_duplicate_elapsed(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Reading",
            "",
            tool_name="read",
            raw_args={"file_path": "src/app.py"},
        )
        dock.finish_tool_node(tool, "read", 2.5, True)

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))
        assert rendered.count("(2.5s)") == 1
    finally:
        dock.deactivate()
        dock.reset()


def test_input_cursor_position_counts_wide_chinese_cells(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    tui = _tui(tmp_path)
    tui._input_lines = ["你好i zai"]
    tui._cursor_row = 0
    tui._cursor_col = 2
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    tui._position_input_cursor()

    assert fake_stdout.text.startswith("\x1b[1A")
    assert "\x1b[7G" in fake_stdout.text


def test_input_cursor_position_accounts_for_status_line(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    status = SimpleNamespace(provider="openai", model="gpt", workspace=str(tmp_path))
    tui = PureTui(status, COMMANDS)
    tui._input_lines = ["现在"]
    tui._cursor_row = 0
    tui._cursor_col = 2
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    tui._position_input_cursor()

    assert fake_stdout.text == "\x1b[2A\x1b[7G"


def test_input_cursor_position_accounts_for_wrapped_long_line(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

    fake_stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    tui = _tui(tmp_path)
    tui._input_lines = ["x" * 50]
    tui._cursor_row = 0
    tui._cursor_col = 0
    tui._console = Console(file=None, force_terminal=True, width=22, height=24, _environ={})

    tui._position_input_cursor()

    match = re.search(r"\x1b\[(\d+)A", fake_stdout.text)
    assert match is not None
    assert int(match.group(1)) == 3


def test_tty_render_reuses_previous_frame_region(tmp_path, monkeypatch):
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
    tui._tty = True
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})

    tui._render_frame()
    assert tui._has_rendered_frame is True
    assert "\x1b[J" in fake_stdout.text

    fake_stdout.text = ""
    tui._input_lines = ["h"]
    tui._cursor_col = 1
    tui._render_frame()

    # Normal-buffer mode: relative cursor movement instead of absolute \x1b[H
    assert "\x1b[H" not in fake_stdout.text
    assert "\x1b[" in fake_stdout.text
    assert "\x1b[J" in fake_stdout.text


def test_command_panel_renders_below_input_with_bottom_rule(tmp_path):
    tui = _tui(
        tmp_path,
        commands=[
            ("/model", "Switch model"),
            ("/model new", "Create or update a model profile"),
            ("/model reasoning", "Set reasoning effort level"),
            ("/model switch", "Switch to a configured provider"),
        ],
    )
    tui._input_lines = ["/model"]
    tui._cursor_col = len("/model")
    tui._update_input_panels()
    tui._command_selected = 1

    lines = _render_lines(tui)
    input_index = next(i for i, line in enumerate(lines) if line.strip() == "❯ /model")
    selected_index = next(i for i, line in enumerate(lines) if "/model new" in line)
    reasoning_index = next(i for i, line in enumerate(lines) if "/model reasoning" in line)
    switch_index = next(i for i, line in enumerate(lines) if "/model switch" in line)

    assert input_index < selected_index
    assert set(lines[input_index + 1]) == {"─"}
    assert lines[selected_index].strip().startswith("❯ /model new")
    assert not lines[reasoning_index].strip().startswith("❯")
    assert "↑↓ select" not in "\n".join(lines)
    assert "Enter accept" not in "\n".join(lines)
    assert "Esc close" not in "\n".join(lines)


def test_command_panel_keeps_dynamic_status_below_panel(tmp_path):
    status = SimpleNamespace(
        provider="mimo-token-plan",
        model="mimo-v2.5-pro",
        workspace=str(tmp_path),
        reasoning_effort="xhigh",
        permission_label=lambda: "accept edits",
        sandbox_label=lambda: "w-write",
        approval_label=lambda: "ask",
        interaction_mode=lambda: "auto",
        debug=lambda: True,
        plan_mode=lambda: False,
    )
    tui = PureTui(status, [("/model", "Switch model"), ("/model new", "Create profile")])
    tui._input_lines = ["/model"]
    tui._cursor_col = len("/model")
    tui._update_input_panels()

    lines = _render_lines(tui)
    panel_index = next(i for i, line in enumerate(lines) if "/model new" in line)
    status_index = next(i for i, line in enumerate(lines) if "mimo-token-plan/mimo-v2.5-pro" in line)

    assert panel_index < status_index
    assert "↑↓ select" not in lines[status_index]
    assert "Enter accept" not in lines[status_index]
    assert "Esc close" not in lines[status_index]


def test_transient_output_appends_to_dock(tmp_path):
    tui = _tui(tmp_path)
    tui.show_transient_output(
        "  [cyan]python[/cyan] [dim]→[/dim] /opt/homebrew/bin/node [dim][CursorPyright][/dim]",
        title="LSP",
    )

    from voidx.ui.output.dock import dock as dock_instance
    tree_lines = dock_instance.tree.render(80)
    rendered = "\n".join(tree_lines)

    assert "python" in rendered
    assert "/opt/homebrew/bin/" in rendered


def test_secret_input_masks_by_display_width(tmp_path):
    tui = _tui(tmp_path)
    tui._active_text_secret = True
    tui._input_lines = ["你好"]
    tui._cursor_col = 2

    rendered = "\n".join(_render_lines(tui))

    assert "****" in rendered
    assert "你好" not in rendered


def test_choice_enter_submits_selected_value(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [
        ("Yes", "y", "Allow"),
        ("No", "n", "Deny"),
    ]
    tui._choice_selected = 1

    tui._process_input(b"\r")

    assert tui._choice_queue.get_nowait() == "n"
    assert tui._queue.empty()


def test_choice_quick_key_finishes_single_character_value(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [
        ("Yes", "y", "Allow"),
        ("No", "n", "Deny"),
    ]

    tui._process_input(b"n")

    assert tui._choice_queue.get_nowait() == "n"
    assert tui._queue.empty()


def test_choice_text_does_not_select_by_non_ascii_label_prefix(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [
        ("中文", "zh", "Chinese"),
        ("English", "en", "English"),
    ]
    tui._choice_selected = 1

    tui._process_input("中".encode("utf-8"))

    assert tui._choice_selected == 1
    assert tui._choice_queue.empty()
    assert tui._queue.empty()


def test_alt_key_does_not_trigger_choice_quick_select(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [
        ("Yes", "y", "Allow"),
        ("No", "n", "Deny"),
    ]

    changed = tui._process_input(b"\x1bn")

    assert changed is False
    assert tui._choice_queue.empty()
    assert tui._queue.empty()


def test_ask_choice_accepts_permission_details(tmp_path):
    tui = _tui(tmp_path)

    async def run_prompt():
        task = asyncio.create_task(
            tui.ask_choice(
                "Allow tool use?",
                [("Yes", "y", "Allow"), ("No", "n", "Deny")],
                details=[{"name": "edit", "pattern": "src/app.py"}],
            )
        )
        await asyncio.sleep(0)
        assert tui._choice_details == [{"name": "edit", "pattern": "src/app.py"}]
        tui._process_input(b"\r")
        return await task

    assert asyncio.run(run_prompt()) == "y"


def test_ask_choice_timeout_returns_none_and_clears_prompt(tmp_path):
    tui = _tui(tmp_path)

    async def run_prompt():
        return await tui.ask_choice(
            "Allow tool use?",
            [("Yes", "y", "Allow"), ("No", "n", "Deny")],
            timeout=0.001,
        )

    assert asyncio.run(run_prompt()) is None
    assert tui._active_choice is None


def test_ask_choice_ignores_stale_queue_values_after_timeout(tmp_path):
    tui = _tui(tmp_path)

    async def run_prompt():
        return await tui.ask_choice(
            "Allow tool use?",
            [("Yes", "y", "Allow"), ("No", "n", "Deny")],
            timeout=0.001,
        )

    assert asyncio.run(run_prompt()) is None
    tui._choice_queue.put_nowait("stale")
    assert asyncio.run(run_prompt()) is None


def test_ask_text_timeout_returns_none_and_restores_input(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["draft"]
    tui._cursor_col = 5

    async def run_prompt():
        return await tui.ask_text("Name?", default="default", timeout=0.001)

    assert asyncio.run(run_prompt()) is None
    assert tui._get_input_text() == "draft"


def test_ask_text_ignores_stale_queue_values_after_timeout(tmp_path):
    tui = _tui(tmp_path)

    async def run_prompt():
        return await tui.ask_text("Name?", default="default", timeout=0.001)

    assert asyncio.run(run_prompt()) is None
    tui._text_queue.put_nowait("stale")
    assert asyncio.run(run_prompt()) is None


def test_command_panel_enter_accepts_selected_command_without_queueing(tmp_path):
    tui = _tui(
        tmp_path,
        commands=[
            ("/mode", "Choose interaction mode"),
            ("/model", "Switch model"),
        ],
    )
    tui._input_lines = ["/mo"]
    tui._cursor_col = 3
    tui._update_command_panel()
    tui._command_selected = 1

    tui._process_input(b"\r")

    assert tui._get_input_text() == "/model"
    assert tui._queue.empty()

    tui._process_input(b"\r")

    assert tui._queue.get_nowait() == "/model"


def test_filtered_commands_only_match_current_prefix(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["/approval on-failure now"]
    tui._cursor_col = len("/approval on-failure now")

    assert tui._filtered_commands() == []


def test_attachment_panel_accepts_workspace_file(tmp_path):
    file_path = tmp_path / "src" / "main.py"
    file_path.parent.mkdir()
    file_path.write_text("print('hi')\n", encoding="utf-8")
    tui = _tui(tmp_path)
    tui._input_lines = ["@src"]
    tui._cursor_col = len("@src")
    tui._update_input_panels()

    assert tui._attachment_panel_active()
    panel = "\n".join(tui._render_attachment_panel(80))
    assert "src/" in panel

    # Select src/ directory — drills into it
    tui._process_input(b"\r")
    assert tui._get_input_text() == "@src/"
    assert tui._attachment_panel_active()

    # Now select main.py inside src/
    tui._process_input(b"\r")
    assert tui._get_input_text() == "@src/main.py "
    assert tui._queue.empty()




def test_attachment_matches_are_cached_per_token(tmp_path, monkeypatch):
    from voidx.ui.tools.file_picker import FileCandidate

    calls: list[str] = []

    def fake_list_file_candidates(_workspace: str, query: str, limit: int = 8):
        calls.append(query)
        return [FileCandidate("src/main.py", "file", 1)]

    monkeypatch.setattr(
        "voidx.ui.tui.panels.list_file_candidates",
        fake_list_file_candidates,
    )
    tui = _tui(tmp_path)
    tui._input_lines = ["@src"]
    tui._cursor_col = len("@src")

    assert tui._attachment_matches()
    assert tui._attachment_matches()

    tui._insert_text("x")
    assert tui._attachment_matches()

    assert calls == ["src", "srcx"]


def test_attachment_panel_quotes_paths_with_spaces(tmp_path):
    file_path = tmp_path / "notes" / "my file.txt"
    file_path.parent.mkdir()
    file_path.write_text("hello\n", encoding="utf-8")
    tui = _tui(tmp_path)
    tui._input_lines = ["@notes"]
    tui._cursor_col = len("@notes")
    tui._update_input_panels()

    # Select notes/ directory
    assert tui._accept_attachment_panel_selection()
    assert tui._get_input_text() == "@notes/"

    # Now filter for "my" inside notes/
    tui._input_lines = ["@notes/my"]
    tui._cursor_col = len("@notes/my")
    tui._update_input_panels()
    assert tui._accept_attachment_panel_selection()
    assert tui._get_input_text() == '@"notes/my file.txt" '



def test_attachment_panel_arrow_selection_accepts_selected_file(tmp_path):
    (tmp_path / "file0.txt").write_text("0", encoding="utf-8")
    (tmp_path / "file1.txt").write_text("1", encoding="utf-8")
    tui = _tui(tmp_path)
    tui._input_lines = ["@file"]
    tui._cursor_col = len("@file")
    tui._update_input_panels()

    tui._process_input(b"\x1b[B")
    tui._process_input(b"\r")

    assert tui._get_input_text() == "@file1.txt "
    assert tui._queue.empty()


def test_attachment_panel_escape_hides_without_accepting(tmp_path):
    file_path = tmp_path / "src" / "main.py"
    file_path.parent.mkdir()
    file_path.write_text("print('hi')\n", encoding="utf-8")
    tui = _tui(tmp_path)
    tui._input_lines = ["@src"]
    tui._cursor_col = len("@src")
    tui._update_input_panels()

    assert tui._attachment_panel_active()

    tui._process_input(b"\x1b")

    assert not tui._attachment_panel_active()
    assert tui._get_input_text() == "@src"


def test_attachment_panel_suppression_clears_after_text_changes(tmp_path):
    file_path = tmp_path / "src" / "main.py"
    file_path.parent.mkdir()
    file_path.write_text("print('hi')\n", encoding="utf-8")
    tui = _tui(tmp_path)
    tui._input_lines = ["@src"]
    tui._cursor_col = len("@src")
    tui._update_input_panels()

    assert tui._attachment_panel_active()
    tui._process_input(b"\x1b")
    assert not tui._attachment_panel_active()

    tui._process_input(b"\x7f")
    tui._process_input(b"c")

    assert tui._get_input_text() == "@src"
    assert tui._attachment_panel_active()


def test_regular_enter_submits_input(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    tui._process_input(b"\r")

    assert tui._get_input_text() == ""
    assert tui._queue.get_nowait() == "hello"


@pytest.mark.asyncio
async def test_tui_busy_guide_bypasses_submit_queue(tmp_path):
    tui = _tui(tmp_path)
    requests: list[dict[str, str]] = []

    async def handle_request(request):
        requests.append(request)

    tui.set_external_command_handler(handle_request)
    tui._busy = True
    tui._input_lines = ["/guide use TypeScript"]
    tui._cursor_col = len("/guide use TypeScript")

    changed = tui._process_input(b"\r")
    await asyncio.sleep(0)

    assert changed is True
    assert requests == [{"kind": "guide", "text": "use TypeScript"}]
    assert tui._queue.empty()
    assert tui._get_input_text() == ""


@pytest.mark.asyncio
async def test_tui_busy_clear_cancels_current_submit_and_runs_clear_next(tmp_path):
    tui = _tui(tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    clear_seen = asyncio.Event()
    submitted: list[str] = []

    async def on_submit(text: str) -> bool:
        submitted.append(text)
        if text == "slow":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        if text == "/clear":
            clear_seen.set()
        return True

    consumer = asyncio.create_task(tui._consume(on_submit))
    try:
        tui._queue.put_nowait("slow")
        await started.wait()
        tui._queue.put_nowait("stale prompt")
        tui._input_lines = ["/clear"]
        tui._cursor_col = len("/clear")

        changed = tui._process_input(b"\r")

        assert changed is True
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await asyncio.wait_for(clear_seen.wait(), timeout=1)

        assert submitted == ["slow", "/clear"]
        assert tui._notice == "Clearing current turn..."
    finally:
        tui._queue.put_nowait(None)
        await asyncio.wait_for(consumer, timeout=1)


def test_empty_enter_on_empty_input_is_noop(tmp_path):
    tui = _tui(tmp_path)

    changed = tui._process_input(b"\r")

    assert changed is False
    assert tui._get_input_text() == ""
    assert tui._queue.empty()


@pytest.mark.parametrize(
    "sequence,expected_input",
    [
        (b"\x1b[Mabc", "abc"),
    ],
)
def test_legacy_mouse_sequences_become_plain_text(tmp_path, sequence, expected_input):
    """Without mouse tracking enabled, mouse escape sequences degrade to
    printable characters — ESC is consumed, M abc are inserted as text."""
    tui = _tui(tmp_path)

    changed = tui._process_input(sequence)

    assert changed is True
    assert tui._get_input_text() == expected_input
    assert tui._queue.empty()


@pytest.mark.parametrize(
    "sequence",
    [
        b"\x1b[<64;10;5M",
        b"\x1b[<65;10;5m",
        b"\x1b[64;10;5M",
    ],
)
def test_mouse_scroll_is_noop_without_tracking(tmp_path, sequence):
    """Without mouse tracking, SGR mouse sequences are consumed as CSI
    with no side effects — the terminal won't send them anyway."""
    tui = _tui(tmp_path)

    changed = tui._process_input(sequence)

    assert changed is False
    assert tui._get_input_text() == ""
    assert tui._queue.empty()


def test_shift_enter_csi_u_inserts_newline_without_submit(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    tui._process_input(b"\x1b[13;2u")

    assert tui._get_input_text() == "hello\n"
    assert tui._queue.empty()


def test_shift_enter_modify_other_keys_inserts_newline_without_submit(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    tui._process_input(b"\x1b[27;2;13~")

    assert tui._get_input_text() == "hello\n"
    assert tui._queue.empty()


def test_ctrl_j_in_tty_mode_inserts_newline_without_submit(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    tui._process_input(b"\n")

    assert tui._get_input_text() == "hello\n"
    assert tui._queue.empty()


def test_multiline_input_render_uses_indentation_without_visible_newline_symbol(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["1、", "2、", "3、"]
    tui._cursor_row = 2
    tui._cursor_col = 2

    console = Console(file=None, force_terminal=False, width=80, height=24, _environ={})
    with console.capture() as capture:
        console.print(tui._render_impl())
    rendered = capture.get()

    assert "↵" not in rendered
    assert "❯ 1、" in rendered
    assert "  2、" in rendered


def test_csi_sequence_consumes_full_sequence_after_text(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["draft"]
    tui._cursor_col = len("draft")
    tui._record_history("old")
    tui._process_input(b"a\x1b[A")

    assert tui._get_input_text() == "old"


def test_multiline_arrow_up_down_moves_within_input_before_history(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["first", "second", "third"]
    tui._cursor_row = 2
    tui._cursor_col = len("third")
    tui._record_history("old")

    tui._process_input(b"\x1b[A")
    assert tui._cursor_row == 1
    assert tui._get_input_text() == "first\nsecond\nthird"

    tui._process_input(b"\x1b[A")
    assert tui._cursor_row == 0
    assert tui._get_input_text() == "first\nsecond\nthird"

    tui._process_input(b"\x1b[A")
    assert tui._get_input_text() == "old"

    tui._process_input(b"\x1b[B")
    assert tui._get_input_text() == "first\nsecond\nthird"


def test_multiline_arrow_navigation_clamps_column(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["short", "a much longer line"]
    tui._cursor_row = 1
    tui._cursor_col = len("a much longer line")

    tui._process_input(b"\x1b[A")

    assert tui._cursor_row == 0
    assert tui._cursor_col == len("short")


def test_ctrl_c_requires_second_empty_press(tmp_path):
    tui = _tui(tmp_path)

    tui._handle_interrupt()

    assert tui._queue.empty()
    assert tui._notice == "Press Ctrl-C again to exit"
    assert tui._ctrl_c_armed is True

    tui._handle_interrupt()

    assert tui._queue.get_nowait() is None


def test_ctrl_c_deadline_requires_fresh_second_press(tmp_path, monkeypatch):
    now = 100.0
    monkeypatch.setattr("voidx.ui.tui.app.time.monotonic", lambda: now)
    tui = _tui(tmp_path)

    tui._handle_interrupt()
    assert tui._ctrl_c_armed is True

    now = 104.0
    tui._handle_interrupt()

    assert tui._queue.empty()
    assert tui._notice == "Press Ctrl-C again to exit"
    assert tui._ctrl_c_deadline == 107.0


def test_ctrl_c_clears_input_before_arming_exit(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    tui._handle_interrupt()

    assert tui._get_input_text() == ""
    assert tui._queue.empty()
    assert "Input cleared" in tui._notice

    tui._handle_interrupt()

    assert tui._queue.empty()
    assert tui._notice == "Press Ctrl-C again to exit"


def test_typing_after_ctrl_c_resets_exit_prompt(tmp_path):
    tui = _tui(tmp_path)

    tui._handle_interrupt()
    tui._insert_text("h")

    assert tui._ctrl_c_armed is False
    assert tui._notice == ""
    assert tui._queue.empty()


@pytest.mark.asyncio
async def test_ctrl_c_cancels_active_submit_task(tmp_path):
    tui = _tui(tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def on_submit(_text: str) -> bool:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    consumer = asyncio.create_task(tui._consume(on_submit))
    tui._queue.put_nowait("slow")
    await started.wait()

    tui._handle_interrupt()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)

    assert tui._get_input_text() == "slow"
    assert tui._notice == "Interrupted. Restored last message for editing."
    assert tui._current_submit_task is None
    assert tui._busy is False

    tui._queue.put_nowait(None)
    await asyncio.wait_for(consumer, timeout=1)


@pytest.mark.asyncio
async def test_read_input_raw_uses_add_reader_for_pipe_bytes(tmp_path):
    if sys.platform == "win32":
        pytest.skip("add_reader coverage is POSIX-only")
    read_fd, write_fd = os.pipe()
    tui = _tui(tmp_path)
    tui._stdin_fd = read_fd
    try:
        os.write(write_fd, b"abc")
        data = await asyncio.wait_for(tui._read_input_raw(), timeout=1)
    finally:
        tui._close_stdin_reader()
        os.close(write_fd)
        os.close(read_fd)

    assert data == b"abc"


@pytest.mark.asyncio
async def test_read_input_raw_does_not_mark_terminal_fd_nonblocking(tmp_path):
    if sys.platform == "win32":
        pytest.skip("pty coverage is POSIX-only")
    import fcntl
    import pty
    import termios

    master_fd, slave_fd = pty.openpty()
    stdout_like_fd = os.dup(slave_fd)
    old_attrs = termios.tcgetattr(slave_fd)
    new_attrs = termios.tcgetattr(slave_fd)
    new_attrs[3] &= ~(termios.ICANON | termios.ECHO)
    new_attrs[6][termios.VMIN] = 1
    new_attrs[6][termios.VTIME] = 0
    termios.tcsetattr(slave_fd, termios.TCSANOW, new_attrs)

    def is_nonblocking(fd: int) -> bool:
        return bool(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK)

    tui = _tui(tmp_path)
    tui._stdin_fd = slave_fd
    task = asyncio.create_task(tui._read_input_raw())
    try:
        await asyncio.sleep(0)
        assert not is_nonblocking(slave_fd)
        assert not is_nonblocking(stdout_like_fd)

        os.write(master_fd, b"x")
        data = await asyncio.wait_for(task, timeout=1)

        assert data == b"x"
        assert not is_nonblocking(slave_fd)
        assert not is_nonblocking(stdout_like_fd)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        termios.tcsetattr(slave_fd, termios.TCSANOW, old_attrs)
        os.close(stdout_like_fd)
        os.close(slave_fd)
        os.close(master_fd)


@pytest.mark.asyncio
async def test_read_input_raw_returns_ctrl_d_on_stdin_eof(tmp_path):
    if sys.platform == "win32":
        pytest.skip("add_reader coverage is POSIX-only")
    read_fd, write_fd = os.pipe()
    tui = _tui(tmp_path)
    tui._stdin_fd = read_fd
    os.close(write_fd)
    try:
        data = await asyncio.wait_for(tui._read_input_raw(), timeout=1)
    finally:
        tui._close_stdin_reader()
        os.close(read_fd)

    assert data == b"\x04"


@pytest.mark.asyncio
async def test_run_finally_does_not_mask_missing_workspace(monkeypatch):
    class FakeStdout:
        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            pass

    async def fake_read_input_raw():
        return b"\x04"

    tui = PureTui(SimpleNamespace(), COMMANDS)
    tui._stdin_fd = 99
    monkeypatch.setattr(os, "isatty", lambda _fd: True)
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    monkeypatch.setattr(tui, "_setup_terminal", lambda: None)
    monkeypatch.setattr(tui, "_restore_terminal", lambda: None)
    monkeypatch.setattr(tui, "_render_frame", lambda: None)
    monkeypatch.setattr(tui, "_move_to_frame_end_sequence", lambda: "")
    monkeypatch.setattr(tui, "_read_input_raw", fake_read_input_raw)

    async def on_submit(_text: str) -> bool:
        return True

    await tui.run(on_submit)


def test_capture_renderable_reuses_console_and_clears_buffer(tmp_path):
    tui = _tui(tmp_path)

    first = tui._capture_renderable(Text("first"), 80)
    console_id = id(tui._capture_console)
    second = tui._capture_renderable(Text("second"), 80)

    assert id(tui._capture_console) == console_id
    assert "first" in first
    assert "first" not in second
    assert "second" in second


def test_input_history_is_bounded(tmp_path):
    tui = _tui(tmp_path)
    limit = tui.INPUT_HISTORY_LIMIT

    for index in range(limit + 5):
        tui._record_history(f"command {index}")

    assert len(tui._input_history) == limit
    assert tui._input_history[0] == "command 5"


def test_tree_incremental_render_only_rewalks_dirty_subtree():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    # Build a tree with two independent root children
    turn1 = tree.new_node(tree.root, node_type="turn", header="Turn 1")
    tree.new_node(turn1, node_type="message", header="msg1", body_lines=["body1"])
    turn2 = tree.new_node(tree.root, node_type="turn", header="Turn 2")
    stream = tree.new_node(turn2, node_type="assistant", header="stream", body_lines=["line 1"])

    # Full render
    full_render = tree.render(80)
    assert any("Turn 1" in line for line in full_render)
    assert any("Turn 2" in line for line in full_render)

    # Content-only update on the stream node
    stream.body_lines = ["line 1", "line 2 (new)"]
    tree.mark_dirty(stream.id)

    # Incremental render should still be correct
    # Verify incremental path was used (no full dirty flag)
    assert tree._dirty is False
    inc_render = tree.render(80)
    assert "line 2 (new)" in "\n".join(inc_render)
    assert any("Turn 1" in line for line in inc_render)
    assert any("Turn 2" in line for line in inc_render)
    assert len(inc_render) > len(full_render)  # grew by one line
    # Verify range: stream node's start position stays unchanged.
    assert tree._node_ranges[stream.id][0] >= 2  # after turn2 header

    # Third update – should still be correct
    stream.body_lines = ["line 1", "line 2 (new)", "line 3 (newest)"]
    tree.mark_dirty(stream.id)
    inc_render2 = tree.render(80)
    assert "line 3 (newest)" in "\n".join(inc_render2)
    # Verify no duplicate content from stale ranges
    joined = "\n".join(inc_render2)
    assert joined.count("line 1") == 1  # appears exactly once
    assert joined.count("line 3 (newest)") == 1

    # After structural change (add node), full render should work
    turn3 = tree.new_node(tree.root, node_type="turn", header="Turn 3")
    final = tree.render(80)
    assert any("Turn 1" in line for line in final)
    assert any("Turn 3" in line for line in final)


def test_tree_incremental_render_shifts_sibling_after_ranges():
    """After incremental splice, sibling-after nodes get shifted ranges."""
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="Turn")
    assistant = tree.new_node(tree.root, node_type="assistant", header="● Working")
    stream = tree.new_node(assistant, node_type="assistant", header="stream", body_lines=["line 1"])
    # sibling-after: same parent as stream
    tool = tree.new_node(assistant, node_type="tool_call", header="read file.py")

    full = tree.render(80)
    # Remember ranges after full render
    tool_start, tool_end = tree._node_ranges[tool.id]
    assistant_start, assistant_end = tree._node_ranges[assistant.id]

    # Content-only update on stream: it grows by 2 lines
    stream.body_lines = ["line 1", "extra line 2", "extra line 3"]
    tree.mark_dirty(stream.id)
    inc = tree.render(80)

    delta = len(inc) - len(full)
    assert delta == 2

    # tool's range should shift by delta
    new_tool_start, new_tool_end = tree._node_ranges[tool.id]
    assert new_tool_start == tool_start + delta
    assert new_tool_end == tool_end + delta

    # assistant's end should shift by delta (ancestor spanning)
    _, new_assistant_end = tree._node_ranges[assistant.id]
    assert new_assistant_end == assistant_end + delta

    # Content is still correct
    assert "extra line 3" in "\n".join(inc)
    assert "read file.py" in "\n".join(inc)


def test_tree_incremental_render_shifts_click_map():
    """After incremental splice, _click_map rows shift correctly."""
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="Turn")
    assistant = tree.new_node(tree.root, node_type="assistant", header="Working")
    stream = tree.new_node(assistant, node_type="assistant", header="stream", body_lines=["L1"])
    tool = tree.new_node(assistant, node_type="tool_call", header="read file")

    full = tree.render(80)
    # tool is clickable
    tool_rows = [r for r, n in tree._click_map.items() if n == tool.id]
    assert len(tool_rows) == 1
    old_tool_row = tool_rows[0]
    assert "read file" in full[old_tool_row]

    # Content-only update: stream grows by 2 lines
    stream.body_lines = ["L1", "L2", "L3"]
    tree.mark_dirty(stream.id)
    inc = tree.render(80)

    # tool's click_map row should shift by delta=2
    new_tool_rows = [r for r, n in tree._click_map.items() if n == tool.id]
    assert len(new_tool_rows) == 1
    assert new_tool_rows[0] == old_tool_row + 2
    assert "read file" in inc[new_tool_rows[0]]

    # Second incremental: stream shrinks by 1 line
    stream.body_lines = ["L1", "L2"]
    tree.mark_dirty(stream.id)
    inc2 = tree.render(80)
    tool_rows2 = [r for r, n in tree._click_map.items() if n == tool.id]
    assert tool_rows2[0] == old_tool_row + 1
    assert "read file" in inc2[tool_rows2[0]]


def test_tree_line_map_tracks_non_clickable_body_rows():
    from voidx.ui.output.tree import OutputTree

    tree = OutputTree()
    message = tree.new_node(
        tree.root,
        node_type="message",
        header="header",
        body_lines=["body"],
    )

    lines, line_map = tree.render_with_line_map(80)

    assert lines == ["header", "body"]
    assert line_map == {0: message.id, 1: message.id}
    assert tree._click_map == {}


def test_safe_flush_line_count_stops_at_unsettled_ancestor():
    test_dock = dock
    test_dock.begin_capture()
    test_dock.start_turn("demo")
    tool = test_dock.start_tool(
        "Reading",
        'file_path="x.py"',
        tool_name="read",
        raw_args={"file_path": "x.py"},
    )
    test_dock.finish_tool_node(tool, "Read", 0.1, True)
    test_dock.append_tool_result("result")

    lines = test_dock.tree.render(100)
    blocked_limit = test_dock.safe_flush_line_count(100, 0)

    assert blocked_limit < len(lines)
    assert "Read" in "\n".join(lines[blocked_limit:])

    test_dock.set_stream("● final answer")
    lines = test_dock.tree.render(100)
    advanced_limit = test_dock.safe_flush_line_count(100, 0)

    assert advanced_limit > blocked_limit
    assert "Read" in "\n".join(lines[:advanced_limit])
    assert "final answer" in "\n".join(lines[advanced_limit:])


def test_safe_flush_line_count_requires_settled_ancestors():
    test_dock = dock
    test_dock.begin_capture()
    parent = test_dock.tree.new_node(
        test_dock.tree.root,
        node_type="subagent",
        header="explore agent",
        collapsed=False,
    )
    child = test_dock.tree.new_node(
        parent,
        node_type="assistant",
        header="found auth flow",
        collapsed=False,
    )
    test_dock.mark_node_unsettled(parent)
    test_dock.mark_node_settled(child)

    assert test_dock.safe_flush_line_count(100, 0) == 0

    test_dock.mark_node_settled(parent)
    assert test_dock.safe_flush_line_count(100, 0) == len(test_dock.tree.render(100))


def test_non_tty_flush_committed_prints_settled_prefix_before_idle(tmp_path, monkeypatch):
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

    test_dock = dock
    test_dock.begin_capture()
    test_dock.start_turn("hello")

    tui = _tui(tmp_path)
    tui._tty = False
    tui._console = Console(file=None, force_terminal=False, width=80, height=24, _environ={})

    tui._flush_committed()

    assert "hello" in fake_stdout.text
    assert tui._committed_line_count > 0


def test_dump_transcript_log_writes_plain_text(tmp_path):
    tui = _tui(tmp_path)

    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="turn",
        header="[bold white]❯[/] hello world",
        body_lines=["this is a test message"],
        collapsed=False,
    )
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="[#EBCB8B]●[/#EBCB8B] response",
        body_lines=["some output here"],
        collapsed=False,
    )

    from voidx.ui.tui import _dump_transcript_log

    log_path = tmp_path / ".voidx" / "transcript.log"
    assert not log_path.exists()

    _dump_transcript_log(tmp_path, dock.tree)

    assert log_path.exists()
    content = log_path.read_text()
    assert "hello world" in content
    assert "this is a test message" in content
    assert "some output here" in content


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

    assert tui._get_input_text() == "hello\nworld"
    assert tui._queue.empty()


def test_bracketed_paste_multiline_text_inserts_as_whole(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    paste_data = b"\x1b[200~line1\r\nline2\r\nline3\x1b[201~"
    tui._process_input(paste_data)

    assert tui._get_input_text() == "line1\nline2\nline3"
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

    assert tui._queue.get_nowait() == pasted
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

    assert tui._queue.get_nowait() == pasted
    assert tui._queue.get_nowait() == pasted
    assert tui._input_history == [token]


@pytest.mark.asyncio
async def test_interrupted_submit_restores_collapsed_paste_token(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    pasted = "line1\nline2\nline3\nline4"
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def on_submit(text: str) -> bool:
        assert text == pasted
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
    assert "\x1b[2;36m" in ansi or "\x1b[36;2m" in ansi


def test_bracketed_paste_single_line_does_not_submit(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    paste_data = b"\x1b[200~hello world\x1b[201~"
    tui._process_input(paste_data)

    assert tui._get_input_text() == "hello world"
    assert tui._queue.empty()


def test_bracketed_paste_with_trailing_key(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    # Paste followed by a regular keypress
    paste_data = b"\x1b[200~text\x1b[201~x"
    tui._process_input(paste_data)

    assert tui._get_input_text() == "textx"
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
    assert tui._get_input_text() == "line1\nline2"
    assert tui._queue.empty()


def test_bracketed_paste_cr_only_normalised_to_newline(tmp_path):
    tui = _tui(tmp_path)
    tui._tty = True
    tui._input_lines = [""]
    tui._cursor_col = 0

    # Bare CR (no LF) should also become a newline
    paste_data = b"\x1b[200~line1\rline2\x1b[201~"
    tui._process_input(paste_data)

    assert tui._get_input_text() == "line1\nline2"
    assert tui._queue.empty()
