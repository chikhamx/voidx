from tui_helpers import *  # noqa: F403

import asyncio
import threading
import time
import pytest
import os
import re
import sys
from types import SimpleNamespace

from rich.cells import cell_len
from rich.console import Console

from voidx.presentation.commands import COMMANDS
from voidx.presentation.output.dock import dock
import voidx_cli.terminal_mixin as terminal_mixin
from voidx_cli import (
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
    assert _rendered_row_count("one\n") == 2
    assert _rendered_row_count("one\ntwo\n") == 3


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
    if sys.platform != "win32":
        assert "line 13" in rendered
    assert "line 19" in rendered




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
        plain_lines = [_rich_plain(line) for line in lines[:3]]
        assert [line.rstrip() for line in plain_lines] == ["❯ 1、你", "  2、好", "  3、你是谁"]
        assert all(cell_len(line) == 100 for line in plain_lines)
        assert turn.body_lines == ["2、好", "3、你是谁"]
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_turn_preserves_long_single_line_input(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        text = "long-input-" + ("x" * 240) + "-tail"
        turn = dock.start_turn(text)

        rendered = "\n".join(dock.tree.render(80))

        assert text in rendered
        assert "tail" in rendered
        assert turn.body_lines == []
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_turn_preserves_long_multiline_input(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        second = "second-" + ("y" * 180)
        third = "third-tail"
        turn = dock.start_turn(f"first\n{second}\n{third}")

        rendered = "\n".join(dock.tree.render(100))

        assert second in rendered
        assert third in rendered
        assert turn.body_lines == [second, third]
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
            'file_path="[cyan]src/voidx/presentation/dock.py[/cyan]"',
            tool_name="read",
            raw_args={"file_path": "src/voidx/presentation/dock.py"},
        )
        dock.finish_tool_node(tool, "read", 0.0, True, "15/151 lines")

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))
        assert 'Read("src/voidx/presentation/dock.py")' in rendered
        assert "15/151 lines" in rendered
        assert "Read 15/151 lines" not in rendered
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


def test_dock_tool_summary_does_not_replace_tool_header(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Running",
            "",
            tool_name="bash",
            raw_args={"command": "git status --short"},
        )
        dock.finish_tool_node(tool, "bash", 0.1, False, "exit 1")

        rendered_lines = [_rich_plain(line).strip() for line in dock.tree.render(120)]
        rendered = "\n".join(rendered_lines)

        assert 'Bash("git status --short")' in rendered
        assert "exit 1" in rendered
        assert "exit 1" not in rendered_lines
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_successful_shell_tool_does_not_show_exit_zero(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Running",
            "",
            tool_name="bash",
            raw_args={"command": "git status --short"},
        )
        dock.finish_tool_node(tool, "bash", 0.1, True, "")

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))

        assert 'Bash("git status --short")' in rendered
        assert "exit 0" not in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_failed_shell_tool_still_shows_nonzero_exit_code(tmp_path):
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Running",
            "",
            tool_name="bash",
            raw_args={"command": "false"},
        )
        dock.finish_tool_node(tool, "bash", 0.1, False, "exit 1")

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))

        assert 'Bash("false")' in rendered
        assert "exit 1" in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_git_tool_header_shows_args_not_path(tmp_path):
    """git tool header should display the args value, not the path field."""
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Git",
            "",
            tool_name="git",
            raw_args={"path": ".", "args": "log --oneline -5"},
        )
        dock.finish_tool_node(tool, "git", 0.1, True)

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))
        assert 'Git("log --oneline -5")' in rendered
        assert 'Git(".")' not in rendered
    finally:
        dock.deactivate()
        dock.reset()


def test_dock_git_tool_finish_detail_no_command_prefix(tmp_path):
    """git finish detail should not duplicate the command name from header."""
    dock.deactivate()
    dock.reset()
    dock.begin_capture()
    try:
        tool = dock.start_tool(
            "Git",
            "",
            tool_name="git",
            raw_args={"path": "", "args": "status --porcelain"},
        )
        dock.finish_tool_node(tool, "git", 0.1, True, "ok")

        rendered = "\n".join(_rich_plain(line) for line in dock.tree.render(120))
        assert 'Git("status --porcelain")' in rendered
        assert "git status" not in rendered
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


@pytest.mark.asyncio
async def test_slow_file_candidate_query_does_not_block_input(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def slow_list_file_candidates(workspace, query, limit=8):
        started.set()
        release.wait(timeout=0.15)
        return [SimpleNamespace(rel_path=f"{query}.py", kind="file", size=0, mtime=0.0)]

    monkeypatch.setattr(
        "voidx_cli.panels.list_file_candidates",
        slow_list_file_candidates,
    )

    timer = threading.Timer(0.15, release.set)
    timer.start()
    try:
        started_at = time.perf_counter()
        tui._process_input(b"@a")
        elapsed = time.perf_counter() - started_at

        assert elapsed < 0.1
        await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
        tui._process_input(b"b")
        assert tui._get_input_text() == "@ab"
        release.set()

        for _ in range(100):
            if any(
                candidate.rel_path == "ab.py"
                for candidate in tui._attachment_matches_cache
            ):
                break
            await asyncio.sleep(0.01)
        assert [candidate.rel_path for candidate in tui._attachment_matches_cache] == ["ab.py"]
    finally:
        release.set()
        timer.cancel()


@pytest.mark.asyncio
async def test_stale_file_candidate_generation_cannot_replace_new_query(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    started_a = threading.Event()
    started_ab = threading.Event()
    release_a = threading.Event()
    release_ab = threading.Event()

    def list_file_candidates(workspace, query, limit=8):
        if query == "a":
            started_a.set()
            release_a.wait(timeout=0.15)
        else:
            started_ab.set()
            release_ab.wait(timeout=1)
        return [SimpleNamespace(rel_path=f"{query}.py", kind="file", size=0, mtime=0.0)]

    monkeypatch.setattr("voidx_cli.panels.list_file_candidates", list_file_candidates)

    try:
        started_at = time.perf_counter()
        tui._process_input(b"@a")
        elapsed = time.perf_counter() - started_at
        assert elapsed < 0.1
        await asyncio.wait_for(asyncio.to_thread(started_a.wait, 1), timeout=1)

        tui._process_input(b"b")
        await asyncio.wait_for(asyncio.to_thread(started_ab.wait, 1), timeout=1)
        release_ab.set()

        for _ in range(100):
            if any(
                candidate.rel_path == "ab.py"
                for candidate in tui._attachment_matches_cache
            ):
                break
            await asyncio.sleep(0.01)
        assert [candidate.rel_path for candidate in tui._attachment_matches_cache] == ["ab.py"]

        release_a.set()
        await asyncio.sleep(0.05)
        assert [candidate.rel_path for candidate in tui._attachment_matches_cache] == ["ab.py"]
    finally:
        release_a.set()
        release_ab.set()


def test_file_candidate_selection_uses_bounded_top_k_and_directory_cache(tmp_path, monkeypatch):
    from heapq import nlargest
    from voidx.presentation.tools import file_picker

    entries = [
        SimpleNamespace(
            name=f"file-{index:04d}.py",
            is_dir=lambda: False,
            stat=lambda index=index: SimpleNamespace(st_mtime=float(index)),
        )
        for index in range(10_000)
    ]
    scans = []
    top_k_calls = []

    class FakeScandir:
        def __enter__(self):
            scans.append(True)
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            scans.append(True)
            return iter(entries)

    def tracked_nlargest(limit, values, *, key):
        top_k_calls.append(limit)
        return nlargest(limit, values, key=key)

    monkeypatch.setattr(file_picker.os, "scandir", lambda path: FakeScandir())
    monkeypatch.setattr(
        file_picker,
        "heapq",
        SimpleNamespace(nlargest=tracked_nlargest),
        raising=False,
    )
    file_picker.invalidate_file_candidate_cache()

    matches = file_picker.list_file_candidates(str(tmp_path), "file-", limit=8)
    assert [candidate.rel_path for candidate in matches] == [
        f"file-{index:04d}.py" for index in range(9_999, 9_991, -1)
    ]
    assert top_k_calls == [8]
    assert len(scans) == 1

    again = file_picker.list_file_candidates(str(tmp_path), "file-", limit=8)
    assert [candidate.rel_path for candidate in again] == [candidate.rel_path for candidate in matches]
    assert len(scans) == 1


def test_file_candidate_cache_invalidates_when_directory_mtime_changes(tmp_path, monkeypatch):
    from voidx.presentation.tools import file_picker

    entries = [
        SimpleNamespace(
            name="old.py",
            is_dir=lambda: False,
            stat=lambda: SimpleNamespace(st_mtime=1.0),
        )
    ]
    scans = []

    class FakeScandir:
        def __iter__(self):
            scans.append(True)
            return iter(entries)

    monkeypatch.setattr(file_picker.os, "scandir", lambda path: FakeScandir())
    file_picker.invalidate_file_candidate_cache()

    file_picker.list_file_candidates(str(tmp_path), "", limit=8)
    first_mtime_ns = tmp_path.stat().st_mtime_ns
    os.utime(tmp_path, ns=(first_mtime_ns, first_mtime_ns + 1_000_000))
    file_picker.list_file_candidates(str(tmp_path), "", limit=8)

    assert len(scans) == 2


@pytest.mark.asyncio
async def test_skill_and_mcp_catalogs_are_reused_until_invalidated(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    skill_calls = 0
    mcp_calls = 0

    class FakeSkillService:
        def enabled_skills(self):
            nonlocal skill_calls
            skill_calls += 1
            return []

    class FakeSkillsApi:
        service = FakeSkillService()

    def skills_provider(workspace):
        return FakeSkillsApi()

    def mcp_provider():
        nonlocal mcp_calls
        mcp_calls += 1
        return []

    tui.set_skills_api_provider(skills_provider)
    tui.set_mcp_catalog_provider(mcp_provider)
    tui._process_input(b"#a")
    for _ in range(100):
        if skill_calls and mcp_calls:
            break
        await asyncio.sleep(0.01)
    tui._process_input(b"b")
    await asyncio.sleep(0.05)

    assert skill_calls == 1
    assert mcp_calls == 1

    tui.invalidate_skill_service_cache()
    tui._process_input(b"c")
    for _ in range(100):
        if skill_calls >= 2 and mcp_calls >= 2:
            break
        await asyncio.sleep(0.01)

    assert skill_calls == 2
    assert mcp_calls == 2


@pytest.mark.asyncio
async def test_panel_query_tasks_are_reaped_on_shutdown(tmp_path, monkeypatch):
    import voidx_cli.panels as panels

    async def blocked_to_thread(*args, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(panels.asyncio, "to_thread", blocked_to_thread)
    tui = _tui(tmp_path)
    tui._input_lines = ["@a"]
    tui._cursor_col = len("@a")
    tui._attachment_matches()

    tui._input_lines = ["#a"]
    tui._cursor_col = len("#a")
    tui._skill_matches()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    attachment_task = tui._attachment_query_task
    skill_task = tui._skill_query_task
    catalog_task = tui._skill_catalog_task

    assert attachment_task is not None and not attachment_task.done()
    assert skill_task is not None and not skill_task.done()
    assert catalog_task is not None and not catalog_task.done()

    await tui._stop_panel_query_tasks()

    assert attachment_task.done()
    assert skill_task.done()
    assert catalog_task.done()
    assert tui._attachment_query_task is None
    assert tui._skill_query_task is None
    assert tui._skill_catalog_task is None
