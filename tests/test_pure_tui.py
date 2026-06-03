import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.llm.usage import UsageStats
from voidx.ui.clipboard_image import ClipboardImageResult
from voidx.ui.commands import COMMANDS
from voidx.ui.dock import BottomInputDock, dock, set_dock
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


def test_status_summary_is_empty_without_model_status(tmp_path):
    tui = _tui(tmp_path)

    assert tui._status_summary(80) == ""
    assert tui._render_hint_lines() == []


def test_terminal_sequences_stay_on_main_screen():
    assert "\x1b[?1049h" not in _ENTER_TERMINAL_SEQUENCE
    assert "\x1b[?1049l" not in _EXIT_TERMINAL_SEQUENCE
    assert "\x1b7" not in _ENTER_TERMINAL_SEQUENCE
    assert "\x1b8" not in _EXIT_TERMINAL_SEQUENCE
    assert "\x1b[?1000l" in _ENTER_TERMINAL_SEQUENCE
    assert "\x1b[?1006l" in _EXIT_TERMINAL_SEQUENCE


def test_rendered_row_count_tracks_terminal_cursor_rows():
    assert _rendered_row_count("") == 0
    assert _rendered_row_count("one") == 1
    assert _rendered_row_count("one\n") == 1
    assert _rendered_row_count("one\ntwo\n") == 2


def test_frame_top_sequence_uses_previous_frame_rows_not_cursor_offset(tmp_path):
    tui = _tui(tmp_path)
    tui._has_rendered_frame = True
    tui._last_frame_rows = 30
    tui._cursor_to_frame_end_lines = 4

    assert tui._move_to_frame_top_sequence() == "\r\x1b[4B\r\x1b[30A\r"


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

    assert "\x1b[7G" in fake_stdout.text


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

    assert fake_stdout.text.startswith("\r\x1b[")
    assert "A\r\x1b[J" in fake_stdout.text


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
    assert set(lines[max(selected_index, reasoning_index, switch_index) + 1]) == {"─"}
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


def test_transient_output_renders_rich_markup(tmp_path):
    tui = _tui(tmp_path)
    tui.show_transient_output(
        "  [cyan]python[/cyan] [dim]→[/dim] /opt/homebrew/bin/node [dim][CursorPyright][/dim]",
        title="LSP",
    )

    rendered = "\n".join(_render_lines(tui))

    assert "LSP" in rendered
    assert "python" in rendered
    assert "/opt/homebrew/bin/" in rendered
    assert "[cyan]" not in rendered
    assert "[dim]" not in rendered


def test_command_output_renders_captured_ansi_without_escape_codes(tmp_path):
    tui = _tui(tmp_path)
    tui.begin_command_output("/")
    tui.append_command_output("\x1b[36m/model\x1b[0m \x1b[2mSwitch model\x1b[0m")

    rendered = "\n".join(_render_lines(tui))

    assert "/model" in rendered
    assert "Switch model" in rendered
    assert "\x1b[" not in rendered


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
    assert "src/main.py" in panel

    tui._process_input(b"\r")

    assert tui._get_input_text() == "@src/main.py "
    assert tui._queue.empty()


def test_attachment_panel_quotes_paths_with_spaces(tmp_path):
    file_path = tmp_path / "notes" / "my file.txt"
    file_path.parent.mkdir()
    file_path.write_text("hello\n", encoding="utf-8")
    tui = _tui(tmp_path)
    tui._input_lines = ["@my"]
    tui._cursor_col = len("@my")
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


def test_regular_enter_submits_input(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["hello"]
    tui._cursor_col = 5

    tui._process_input(b"\r")

    assert tui._get_input_text() == ""
    assert tui._queue.get_nowait() == "hello"


def test_empty_enter_on_empty_input_is_noop(tmp_path):
    tui = _tui(tmp_path)

    changed = tui._process_input(b"\r")

    assert changed is False
    assert tui._get_input_text() == ""
    assert tui._queue.empty()


@pytest.mark.parametrize(
    "sequence",
    [
        b"\x1b[<64;10;5M",
        b"\x1b[<65;10;5m",
        b"\x1b[Mabc",
        b"\x1b[64;10;5M",
    ],
)
def test_mouse_reporting_sequences_are_ignored(tmp_path, sequence):
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
    monkeypatch.setattr("voidx.ui.tui.time.monotonic", lambda: now)
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


def test_paste_clipboard_image_inserts_image_token(tmp_path, monkeypatch):
    def fake_paste(_workspace: str) -> ClipboardImageResult:
        return ClipboardImageResult(
            status="ok",
            message="Pasted image",
            rel_path=".voidx/attachments/clip.png",
            size=123,
        )

    monkeypatch.setattr("voidx.ui.tui.paste_clipboard_image_from_system", fake_paste)
    tui = _tui(tmp_path)

    result = tui.paste_clipboard_image()

    assert result.ok
    assert tui._get_input_text() == "[image-clip] "
    assert tui._notice == "Pasted image"
