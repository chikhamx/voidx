from tui_helpers import *  # noqa: F403

import asyncio
import contextlib
import os
import sys
from types import SimpleNamespace

import pytest
from rich.console import Console

from voidx.config import Settings
from voidx.ui.commands import COMMANDS
from voidx_cli import PureTui


def _write_skill(workspace, name: str, description: str) -> None:
    skill_dir = workspace / ".voidx" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nSkill body",
        encoding="utf-8",
    )


def test_choice_enter_submits_selected_value(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [("y", "y", ""), ("n", "n", "")]
    tui._choice_selected = 1

    tui._process_input(b"\r")

    assert tui._choice_queue.get_nowait() == "n"
    assert tui._queue.empty()


def test_choice_quick_key_finishes_single_character_value(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [("y", "y", ""), ("n", "n", "")]

    tui._process_input(b"n")

    assert tui._choice_queue.get_nowait() == "n"
    assert tui._queue.empty()


def test_choice_text_does_not_select_by_non_ascii_label_prefix(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [("zh", "zh", ""), ("en", "en", "")]
    tui._choice_selected = 1

    tui._process_input("中".encode("utf-8"))

    assert tui._choice_selected == 1
    assert tui._choice_queue.empty()
    assert tui._queue.empty()


def test_alt_key_does_not_trigger_choice_quick_select(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [("y", "y", ""), ("n", "n", "")]

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
                ["y", "n"],
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
            ["y", "n"],
            timeout=0.001,
        )

    assert asyncio.run(run_prompt()) is None
    assert tui._active_choice is None


def test_ask_choice_ignores_stale_queue_values_after_timeout(tmp_path):
    tui = _tui(tmp_path)

    async def run_prompt():
        return await tui.ask_choice(
            "Allow tool use?",
            ["y", "n"],
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



def test_text_prompt_expands_paste_tokens(tmp_path):
    """Pasted content in the clarify/text-prompt input must be expanded to
    actual text, not submitted as a literal [Pasted text #N ...] token."""
    tui = _tui(tmp_path)
    tui._active_text_prompt = "Question?"
    display = tui._register_text_paste("hello world")
    tui._input_lines = [display]
    tui._cursor_col = len(display)

    tui._submit_text_prompt()

    result = tui._text_queue.get_nowait()
    # Paste tokens should be expanded to actual content, not left as
    # literal [Pasted text #N ...] tokens.
    assert "[Pasted text" not in result
    assert "hello world" in result
    # <pasted> wrapper tags must be stripped — clarify answers are plain text.
    assert "<pasted>" not in result
    assert "</pasted>" not in result
    # Paste entries should be cleaned up after submit.
    assert tui._paste_entries == []

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


def test_unknown_slash_command_clears_input_without_queueing(tmp_path):
    tui = _tui(tmp_path)
    tui._input_lines = ["/zzz"]
    tui._cursor_col = len("/zzz")
    tui._update_command_panel()

    tui._process_input(b"\r")

    assert tui._get_input_text() == ""
    assert tui._queue.empty()


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
        "voidx_cli.panels.list_file_candidates",
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
    # Ensure deterministic mtime ordering: file0 newer than file1,
    # so file0.txt is index 0 and file1.txt is index 1 after mtime sort.
    file0_mtime = (tmp_path / "file0.txt").stat().st_mtime
    (tmp_path / "file1.txt").touch()
    os.utime(tmp_path / "file1.txt", (file0_mtime - 2, file0_mtime - 2))

    tui = _tui(tmp_path)
    tui._input_lines = ["@file"]
    tui._cursor_col = len("@file")
    tui._update_input_panels()

    tui._process_input(b"\x1b[B")
    tui._process_input(b"\r")

    # Down arrow moves selection from index 0 (file0.txt) to index 1 (file1.txt).
    assert tui._get_input_text() == "@file1.txt "
    assert tui._queue.empty()


def test_skill_panel_accepts_project_skill(tmp_path):
    _write_skill(tmp_path, "docs", "docs helper")
    Settings(str(tmp_path)).set_skill_auto("docs", True)
    tui = _tui(tmp_path)
    tui._input_lines = ["#do"]
    tui._cursor_col = len("#do")
    tui._update_input_panels()

    assert tui._skill_panel_active()
    panel = "\n".join(tui._render_skill_panel(100))
    assert "docs" in panel
    assert "[auto]" in panel
    assert "project" in panel
    assert "docs helper" in panel

    tui._process_input(b"\r")

    assert tui._get_input_text() == "$docs "
    assert tui._queue.empty()


def test_skill_panel_arrow_selection_accepts_selected_skill(tmp_path):
    _write_skill(tmp_path, "docs", "docs helper")
    _write_skill(tmp_path, "sql-review", "Reviews SQL")
    tui = _tui(tmp_path)
    tui._input_lines = ["#"]
    tui._cursor_col = len("#")
    tui._update_input_panels()

    tui._process_input(b"\x1b[B")
    tui._process_input(b"\r")

    assert tui._get_input_text() == "$sql-review "
    assert tui._queue.empty()


def test_skill_panel_escape_hides_until_text_changes(tmp_path):
    _write_skill(tmp_path, "docs", "docs helper")
    tui = _tui(tmp_path)
    tui._input_lines = ["#do"]
    tui._cursor_col = len("#do")
    tui._update_input_panels()

    assert tui._skill_panel_active()

    tui._process_input(b"\x1b")

    assert not tui._skill_panel_active()
    assert tui._get_input_text() == "#do"

    tui._process_input(b"\x7f")
    tui._process_input(b"o")

    assert tui._get_input_text() == "#do"
    assert tui._skill_panel_active()


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


