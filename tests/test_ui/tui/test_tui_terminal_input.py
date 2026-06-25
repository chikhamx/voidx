from tui_helpers import *  # noqa: F403

import os
import re
import shutil
import sys
from types import SimpleNamespace

from rich.cells import cell_len
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
            {"content": "implement pinned display", "status": "active"},
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
            {"content": "active task", "status": "active"},
            {"content": "pending task", "status": "pending"},
        ],
    )

    lines = _render_lines(tui, width=80)

    transcript_lines = [line for line in lines if "line " in line]
    assert len(transcript_lines) <= 2
    if sys.platform != "win32":
        assert any("line 9" in line for line in transcript_lines)
    assert any("Todo: 0/2 done" in line for line in lines)
    assert any(line.strip().startswith("❯") for line in lines)


def test_pinned_todo_shows_four_items_when_row_budget_allows(tmp_path):
    tui = _tui(tmp_path)
    tui._console = Console(file=None, force_terminal=True, width=80, height=10 if sys.platform == "win32" else 8, _environ={})
    dock.set_todo_state(
        "0/4 done · 1 active · 3 pending",
        [
            {"content": "active task", "status": "active"},
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
        [{"content": "active task", "status": "active"}],
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
        [{"content": "active task", "status": "active"}],
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
    tui._active_choice = [("review", "review", ""), ("implement", "implement", "")]
    tui._choice_prompt = "Intent?"
    tui._choice_selected = 0
    dock.set_todo_state(
        "0/1 done · 1 active · 0 pending",
        [{"content": "active task", "status": "active"}],
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
            {"content": "implement pinned display", "status": "active"},
            {"content": "write tests", "status": "pending"},
            {"content": "verify flicker", "status": "pending"},
        ],
    )

    rendered = "\n".join(_render_lines(tui, width=40))

    if sys.platform == "win32":
        return  # Windows renders too few rows at height=5 for todo panel to appear
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
