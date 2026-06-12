from tests.tui_helpers import *  # noqa: F403

import os
import re
import shutil
import sys
from types import SimpleNamespace

from rich.console import Console

from voidx.ui.commands import COMMANDS
from voidx.ui.output.dock import dock
import voidx.ui.tui.terminal_mixin as terminal_mixin
from voidx.ui.tui import (
    PureTui,
    _ENTER_TERMINAL_SEQUENCE,
    _EXIT_TERMINAL_SEQUENCE,
    _rendered_row_count,
)

def test_terminal_sequences_stay_on_normal_buffer():
    # Alternate screen NOT used — terminal handles scrollback natively
    assert "\x1b[?1049h" not in _ENTER_TERMINAL_SEQUENCE
    assert "\x1b[?1049l" not in _EXIT_TERMINAL_SEQUENCE
    assert "\x1b[?1000l" in _ENTER_TERMINAL_SEQUENCE
    assert "\x1b[?1006l" in _EXIT_TERMINAL_SEQUENCE


class _FakeKernel32:
    def __init__(
        self,
        mode: int = 0,
        *,
        get_ok: bool = True,
        set_ok: bool = True,
    ) -> None:
        self.mode = mode
        self.get_ok = get_ok
        self.set_ok = set_ok
        self.handles: list[int] = []
        self.set_modes: list[int] = []

    def GetStdHandle(self, handle: int) -> int:
        self.handles.append(handle)
        return 123

    def GetConsoleMode(self, handle: int, mode_ptr) -> int:
        if not self.get_ok:
            return 0
        mode_ptr._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle: int, mode: int) -> int:
        if not self.set_ok:
            return 0
        self.set_modes.append(int(mode))
        self.mode = int(mode)
        return 1


def test_windows_enable_virtual_terminal_processing_sets_mode():
    kernel32 = _FakeKernel32(mode=0)

    original = terminal_mixin._enable_windows_virtual_terminal_processing(kernel32)

    assert original == 0
    assert kernel32.handles == [terminal_mixin._STD_OUTPUT_HANDLE]
    assert kernel32.set_modes == [
        terminal_mixin._ENABLE_VIRTUAL_TERMINAL_PROCESSING
    ]


def test_windows_enable_virtual_terminal_processing_keeps_existing_mode():
    mode = terminal_mixin._ENABLE_VIRTUAL_TERMINAL_PROCESSING | 0x0001
    kernel32 = _FakeKernel32(mode=mode)

    original = terminal_mixin._enable_windows_virtual_terminal_processing(kernel32)

    assert original == mode
    assert kernel32.set_modes == []


def test_windows_restore_console_mode_restores_original_mode():
    kernel32 = _FakeKernel32(mode=0)

    restored = terminal_mixin._restore_windows_console_mode(7, kernel32)

    assert restored is True
    assert kernel32.set_modes == [7]


def test_windows_console_mode_helpers_ignore_non_console():
    assert (
        terminal_mixin._enable_windows_virtual_terminal_processing(
            _FakeKernel32(get_ok=False)
        )
        is None
    )
    assert (
        terminal_mixin._enable_windows_virtual_terminal_processing(
            _FakeKernel32(set_ok=False)
        )
        is None
    )
    assert terminal_mixin._restore_windows_console_mode(None, _FakeKernel32()) is False


def test_non_windows_terminal_setup_still_uses_termios(tmp_path, monkeypatch):
    class FakeTermios:
        ECHO = 0x0001
        ICANON = 0x0002
        ISIG = 0x0004
        IEXTEN = 0x0008
        IGNBRK = 0x0010
        ICRNL = 0x0020
        BRKINT = 0x0040
        VMIN = 0
        VTIME = 1
        VLNEXT = 2
        TCSADRAIN = 0

        def __init__(self) -> None:
            self.set_attrs: list[list] = []

        def tcgetattr(self, fd: int) -> list:
            return [
                self.IGNBRK | self.ICRNL,
                0,
                0,
                self.ECHO | self.ICANON | self.ISIG | self.IEXTEN,
                0,
                0,
                [0, 0, 1],
            ]

        def tcsetattr(self, fd: int, when: int, attrs: list) -> None:
            self.set_attrs.append(attrs)

    fake_termios = FakeTermios()
    monkeypatch.setattr(terminal_mixin, "termios", fake_termios)
    monkeypatch.setattr(terminal_mixin.os, "isatty", lambda fd: True)
    tui = _tui(tmp_path)
    tui._stdin_fd = 99

    tui._setup_terminal()

    assert tui._old_termios is not None
    assert len(fake_termios.set_attrs) == 1
    assert fake_termios.set_attrs[0][6][fake_termios.VMIN] == 1
    assert fake_termios.set_attrs[0][6][fake_termios.VTIME] == 0


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


def test_skill_panel_reuses_candidate_service_between_queries(tmp_path, monkeypatch):
    import voidx.ui.tui.panels as panels

    services = []

    def fake_list_skill_candidates(workspace, query, limit=8, *, service=None):
        del workspace, query, limit
        services.append(service)
        return []

    monkeypatch.setattr(panels, "list_skill_candidates", fake_list_skill_candidates)
    tui = _tui(tmp_path)

    tui._input_lines = ["#d"]
    tui._cursor_col = len("#d")
    tui._skill_matches()
    tui._input_lines = ["#do"]
    tui._cursor_col = len("#do")
    tui._skill_matches()

    assert services[0] is not None
    assert services[0] is services[1]


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

    # Stable second frames update only changed rows instead of clearing
    # the whole live frame.
    assert "\x1b[H" not in fake_stdout.text
    assert "\x1b[" in fake_stdout.text
    assert "\x1b[J" not in fake_stdout.text
    assert "\x1b[K" in fake_stdout.text


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


def test_pinned_todo_renders_above_input_and_status(tmp_path):
    status = SimpleNamespace(
        provider="mimo",
        model="mimo-v2.5",
        workspace=str(tmp_path),
    )
    tui = PureTui(status, COMMANDS)
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    dock.tree.new_node(
        parent=dock.tree.root,
        node_type="message",
        header="transcript line",
        collapsed=False,
        status="done",
    )
    dock.set_todo_state(
        "0/2 done · 1 active · 1 pending",
        [
            {"content": "implement pinned display", "status": "in_progress"},
            {"content": "write tests", "status": "pending"},
        ],
    )
    tui._input_lines = ["hello"]
    tui._cursor_col = len("hello")

    lines = _render_lines(tui, width=80)

    transcript_index = next(i for i, line in enumerate(lines) if "transcript line" in line)
    todo_index = next(i for i, line in enumerate(lines) if "Todo: 0/2 done" in line)
    input_index = next(i for i, line in enumerate(lines) if line.strip() == "❯ hello")
    status_index = next(i for i, line in enumerate(lines) if "mimo/mimo-v2.5" in line)
    assert transcript_index < todo_index < input_index < status_index


def test_pinned_todo_reduces_transcript_body_limit(tmp_path):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=8, _environ={})
    for index in range(10):
        dock.tree.new_node(
            parent=dock.tree.root,
            node_type="message",
            header=f"line {index}",
            collapsed=False,
            status="done",
        )
    dock.set_todo_state(
        "0/2 done · 1 active · 1 pending",
        [
            {"content": "active task", "status": "in_progress"},
            {"content": "pending task", "status": "pending"},
        ],
    )

    lines = _render_lines(tui, width=80)

    transcript_lines = [line for line in lines if "line " in line]
    assert len(transcript_lines) <= 2
    assert any("line 9" in line for line in transcript_lines)
    assert any("Todo: 0/2 done" in line for line in lines)
    assert any(line.strip().startswith("❯") for line in lines)


def test_pinned_todo_shows_four_items_when_row_budget_allows(tmp_path):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=8, _environ={})
    dock.set_todo_state(
        "0/4 done · 1 active · 3 pending",
        [
            {"content": "active task", "status": "in_progress"},
            {"content": "pending task 1", "status": "pending"},
            {"content": "pending task 2", "status": "pending"},
            {"content": "pending task 3", "status": "pending"},
        ],
    )

    rendered = "\n".join(_render_lines(tui, width=80))

    assert "Todo: 0/4 done" in rendered
    assert "active task" in rendered
    assert "pending task 1" in rendered
    assert "pending task 2" in rendered
    assert "pending task 3" in rendered
    assert "more todos" not in rendered


def test_pinned_todo_not_in_bottom_impl(tmp_path):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    dock.set_todo_state(
        "0/1 done · 1 active · 0 pending",
        [{"content": "active task", "status": "in_progress"}],
    )

    ansi = tui._capture_renderable(tui._render_bottom_impl(), tui._frame_width())

    assert "Todo:" not in ansi
    assert "active task" not in ansi


def test_input_region_render_still_uses_bottom_only_with_pinned_todo(tmp_path, monkeypatch):
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
        status="done",
    )
    dock.set_todo_state(
        "0/1 done · 1 active · 0 pending",
        [{"content": "active task", "status": "in_progress"}],
    )

    tui._render_frame()
    assert "Todo:" in fake_stdout.text
    assert "startup banner" in fake_stdout.text

    fake_stdout.text = ""
    assert tui._process_input(b"x") is True
    tui._render_after_input()

    assert "Todo:" not in fake_stdout.text
    assert "startup banner" not in fake_stdout.text
    assert "x" in fake_stdout.text


def test_choice_selection_only_render_still_works_with_pinned_todo(tmp_path, monkeypatch):
    fake_stdout = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    tui = _tui(tmp_path)
    tui._tty = True
    tui._has_rendered_frame = True
    tui._last_bottom_start_row = 7
    tui._last_frame_rows = 14
    tui._console = Console(file=None, force_terminal=True, width=80, height=24, _environ={})
    tui._active_choice = [
        ("Review", "review", "Inspect the design"),
        ("Implement", "implement", "Apply the change"),
    ]
    tui._choice_prompt = "Intent?"
    tui._choice_selected = 0
    dock.set_todo_state(
        "0/1 done · 1 active · 0 pending",
        [{"content": "active task", "status": "in_progress"}],
    )
    ansi = tui._capture_renderable(tui._render_bottom_impl(), tui._frame_width())
    tui._last_bottom_rows = _rendered_row_count(ansi)

    tui._choice_selected = 1

    assert tui._render_choice_selection_region() is True
    assert "\x1b[J" not in fake_stdout.text
    assert "Todo:" not in fake_stdout.text


def test_pinned_todo_summary_only_on_tiny_height_or_width(tmp_path):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=40, height=5, _environ={})
    dock.set_todo_state(
        "0/3 done · 1 active · 2 pending",
        [
            {"content": "implement pinned display", "status": "in_progress"},
            {"content": "write tests", "status": "pending"},
            {"content": "verify flicker", "status": "pending"},
        ],
    )

    rendered = "\n".join(_render_lines(tui, width=40))

    assert "Todo: 0/3 done" in rendered
    assert "implement pinned display" not in rendered
    assert "write tests" not in rendered


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
