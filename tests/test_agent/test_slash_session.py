from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.ui.commands import COMMANDS
from voidx.ui.output.diff import make_file_diff
from voidx.ui.session import session_tracker


class FakeChoiceApp:
    def __init__(self, result: str | None) -> None:
        self.result = result
        self.prompt = ""
        self.choices = []

    async def ask_choice(self, prompt, choices):
        self.prompt = prompt
        self.choices = choices
        return self.result


@pytest.fixture(autouse=True)
def clear_session_tracker():
    session_tracker.clear()
    yield
    session_tracker.clear()


def _graph(app=None):
    return SimpleNamespace(_app=app)


def _capture_output(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "voidx.agent.slash.session.ui.print",
        lambda text="": output.append(str(text)),
    )
    monkeypatch.setattr(
        "voidx.agent.slash.session.ui.error",
        lambda text="": output.append(f"ERROR: {text}"),
    )
    return output


@pytest.mark.asyncio
async def test_rollback_command_restores_files_after_confirmation(tmp_path, monkeypatch):
    output = _capture_output(monkeypatch)
    existing = tmp_path / "existing.py"
    existing.write_text("old\n", encoding="utf-8")
    created = tmp_path / "created.py"

    session_tracker.begin_turn(str(tmp_path))
    session_tracker.capture_file("existing.py", str(tmp_path))
    session_tracker.capture_file("created.py", str(tmp_path))
    existing.write_text("new\n", encoding="utf-8")
    created.write_text("hello\n", encoding="utf-8")
    session_tracker.record_diff(make_file_diff("existing.py", "old\n", "new\n"))
    session_tracker.record_diff(
        make_file_diff("created.py", "", "hello\n", old_label="/dev/null", new_label="b/created.py")
    )
    session_tracker.finish_turn()

    app = FakeChoiceApp(result="yes")

    assert await SlashHandler(_graph(app)).dispatch("/rollback") is True

    assert existing.read_text(encoding="utf-8") == "old\n"
    assert not created.exists()
    assert session_tracker.has_rollbackable_changes is False
    assert app.prompt == "Rollback these changes?"
    assert app.choices[0][1] == "no"
    assert app.choices[1][1] == "yes"
    assert any("Restored:" in line for line in output)
    assert any("Removed:" in line for line in output)


@pytest.mark.asyncio
async def test_rollback_command_cancel_keeps_files_and_snapshots(tmp_path, monkeypatch):
    output = _capture_output(monkeypatch)
    target = tmp_path / "target.py"
    target.write_text("old\n", encoding="utf-8")

    session_tracker.begin_turn(str(tmp_path))
    session_tracker.capture_file("target.py", str(tmp_path))
    target.write_text("new\n", encoding="utf-8")
    session_tracker.record_diff(make_file_diff("target.py", "old\n", "new\n"))
    session_tracker.finish_turn()

    assert await SlashHandler(_graph(FakeChoiceApp(result="no"))).dispatch("/rollback") is True

    assert target.read_text(encoding="utf-8") == "new\n"
    assert session_tracker.has_rollbackable_changes is True
    assert any("Rollback cancelled." in line for line in output)


@pytest.mark.asyncio
async def test_rollback_command_uses_snapshot_only_changes(tmp_path, monkeypatch):
    output = _capture_output(monkeypatch)
    target = tmp_path / "target.py"
    target.write_text("old\n", encoding="utf-8")

    session_tracker.begin_turn(str(tmp_path))
    session_tracker.capture_file("target.py", str(tmp_path))
    target.write_text("new\n", encoding="utf-8")
    session_tracker.finish_turn()

    assert await SlashHandler(_graph(FakeChoiceApp(result="yes"))).dispatch("/rollback") is True

    assert target.read_text(encoding="utf-8") == "old\n"
    assert any("snapshot only" in line for line in output)


@pytest.mark.asyncio
async def test_rollback_command_no_changes_prints_message(monkeypatch):
    output = _capture_output(monkeypatch)

    assert await SlashHandler(_graph(FakeChoiceApp(result="yes"))).dispatch("/rollback") is True

    assert output == ["[dim]No file changes to roll back.[/dim]"]


def test_rollback_command_is_in_palette():
    assert ("/rollback", "Revert file changes from the current turn") in COMMANDS


@pytest.mark.asyncio
async def test_guide_command_submits_pending_guidance():
    calls: list[str] = []
    graph = SimpleNamespace(
        submit_guidance=lambda text: calls.append(text) or True,
    )

    assert await SlashHandler(graph).dispatch("/guide keep patch small") is True

    assert calls == ["keep patch small"]


@pytest.mark.asyncio
async def test_guide_command_without_text_prints_usage(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "voidx.agent.slash.guide.ui.print",
        lambda text="": output.append(str(text)),
    )

    assert await SlashHandler(SimpleNamespace()).dispatch("/guide") is True

    assert output == ["[dim]Usage: /guide <guidance for the next agent step>[/dim]"]


def test_guide_command_is_in_palette():
    assert ("/guide", "Add guidance to the running agent turn") in COMMANDS
