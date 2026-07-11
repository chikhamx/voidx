from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


from voidx.agent.slash import SlashHandler
from voidx.agent.slash.host import SlashHostAdapter
import voidx.memory.store as store
from voidx.memory.session import MessageRow, create_session, delete_session, get_session, save_message
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


class SequencedChoiceApp:
    def __init__(self, results: list[str | None]) -> None:
        self.results = results
        self.prompts: list[str] = []
        self.choices_history = []

    async def ask_choice(self, prompt, choices):
        self.prompts.append(prompt)
        self.choices_history.append(choices)
        return self.results.pop(0) if self.results else None


@pytest.fixture(autouse=True)
def clear_session_tracker():
    session_tracker.clear()
    yield
    session_tracker.clear()


@pytest.fixture
def isolated_memory_store(tmp_path):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    previous_data_dir = store.DATA_DIR
    store.DATA_DIR = tmp_path / ".voidx"
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = previous_data_dir


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
async def test_session_del_dry_run_lists_candidates_without_deleting(monkeypatch, isolated_memory_store):
    output = _capture_output(monkeypatch)
    old_session = await create_session(workspace="/tmp/old-workspace")
    recent_session = await create_session()
    try:
        now = datetime.now(timezone.utc)
        await save_message(MessageRow(session_id=old_session.id, role="user", content="old"))
        await store._execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Old Session", (now - timedelta(days=30)).isoformat(), old_session.id),
        )
        await store._execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Recent Session", (now - timedelta(days=1)).isoformat(), recent_session.id),
        )

        assert await SlashHandler(_graph()).dispatch("/session del --dry-run 7d") is True

        assert any("Dry run" in line for line in output)
        assert any(old_session.id[:8] in line and "Old Session" in line for line in output)
        assert any("/tmp/old-workspace" in line for line in output)
        assert all(recent_session.id[:8] not in line for line in output)
        assert await get_session(old_session.id) is not None
        assert await get_session(recent_session.id) is not None
    finally:
        await delete_session(old_session.id)
        await delete_session(recent_session.id)


@pytest.mark.asyncio
async def test_session_del_cancel_keeps_candidates(monkeypatch, isolated_memory_store):
    output = _capture_output(monkeypatch)
    old_session = await create_session()
    recent_session = await create_session()
    try:
        await save_message(MessageRow(session_id=old_session.id, role="user", content="old"))
        await store._execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Old Session", "2000-01-01T00:00:00+00:00", old_session.id),
        )
        await store._execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Future Session", "2999-01-01T00:00:00+00:00", recent_session.id),
        )

        app = FakeChoiceApp(result="no")

        assert await SlashHandler(_graph(app)).dispatch("/session del 7d") is True

        assert app.prompt == "Delete these sessions?"
        assert app.choices[0][1] == "no"
        assert app.choices[1][1] == "yes"
        assert any("Delete preview" in line for line in output)
        assert any("Deletion cancelled." in line for line in output)
        assert await get_session(old_session.id) is not None
        assert await get_session(recent_session.id) is not None
    finally:
        await delete_session(old_session.id)
        await delete_session(recent_session.id)


@pytest.mark.asyncio
async def test_session_del_confirm_deletes_candidates_only(monkeypatch, isolated_memory_store):
    output = _capture_output(monkeypatch)
    old_session = await create_session()
    recent_session = await create_session()
    try:
        await save_message(MessageRow(session_id=old_session.id, role="user", content="old"))
        await save_message(MessageRow(session_id=recent_session.id, role="user", content="recent"))
        await store._execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Old Session", "2000-01-01T00:00:00+00:00", old_session.id),
        )
        await store._execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Future Session", "2999-01-01T00:00:00+00:00", recent_session.id),
        )

        assert await SlashHandler(_graph(FakeChoiceApp(result="yes"))).dispatch("/session del 7d") is True

        assert any("Deleted 1 session(s)" in line for line in output)
        assert await get_session(old_session.id) is None
        assert await get_session(recent_session.id) is not None
    finally:
        await delete_session(old_session.id)
        await delete_session(recent_session.id)


@pytest.mark.asyncio
async def test_session_del_without_scope_asks_for_scope_before_confirm(monkeypatch, isolated_memory_store):
    output = _capture_output(monkeypatch)
    old_session = await create_session()
    recent_session = await create_session()
    try:
        await save_message(MessageRow(session_id=old_session.id, role="user", content="old"))
        await store._execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Old Session", "2000-01-01T00:00:00+00:00", old_session.id),
        )
        await store._execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Future Session", "2999-01-01T00:00:00+00:00", recent_session.id),
        )

        app = SequencedChoiceApp(["7d", "yes"])

        assert await SlashHandler(_graph(app)).dispatch("/session del") is True

        assert app.prompts == ["Delete sessions older than:", "Delete these sessions?"]
        assert app.choices_history[0][0][1] == "7d"
        assert app.choices_history[0][-1][1] == "cancel"
        assert any("Deleted 1 session(s)" in line for line in output)
        assert await get_session(old_session.id) is None
        assert await get_session(recent_session.id) is not None
    finally:
        await delete_session(old_session.id)
        await delete_session(recent_session.id)


@pytest.mark.asyncio
async def test_session_del_without_scope_can_cancel_scope_selection(monkeypatch, isolated_memory_store):
    output = _capture_output(monkeypatch)
    session = await create_session()
    try:
        app = SequencedChoiceApp(["cancel"])

        assert await SlashHandler(_graph(app)).dispatch("/session del") is True

        assert app.prompts == ["Delete sessions older than:"]
        assert any("Deletion cancelled." in line for line in output)
        assert await get_session(session.id) is not None
    finally:
        await delete_session(session.id)


def test_session_del_dry_run_command_is_in_palette():
    assert ("/session del --dry-run", "Preview session deletion candidates") in COMMANDS


def test_session_del_command_is_in_palette():
    assert ("/session del", "Delete old saved sessions") in COMMANDS


@pytest.mark.asyncio
async def test_session_list_alias_lists_saved_sessions(monkeypatch, isolated_memory_store):
    output = _capture_output(monkeypatch)
    session = await create_session()
    try:
        await store._execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Listed Session", "2026-06-15T00:00:00+00:00", session.id),
        )

        assert await SlashHandler(_graph()).dispatch("/session list") is True

        assert any("Sessions:" in line for line in output)
        assert any(session.id[:8] in line and "Listed Session" in line for line in output)
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_session_new_alias_clears_current_session(monkeypatch):
    output = _capture_output(monkeypatch)
    calls: list[str] = []

    async def clear_current_session() -> bool:
        calls.append("clear")
        return True

    async def show_startup(**kwargs) -> bool:
        calls.append(f"startup:{kwargs}")
        return True

    graph = SimpleNamespace(
        clear_current_session=clear_current_session,
        show_startup=show_startup,
    )

    assert await SlashHandler(graph).dispatch("/session new") is True

    assert calls == ["clear", "startup:{'prefer_direct': True}"]
    assert output == []


@pytest.mark.asyncio
async def test_session_resume_alias_resumes_saved_session(monkeypatch, isolated_memory_store):
    output = _capture_output(monkeypatch)
    session = await create_session()
    calls: list[str] = []
    try:
        await store._execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Resume Me", "2026-06-15T00:00:00+00:00", session.id),
        )

        async def resume_session(resumed) -> bool:
            calls.append(f"resume:{resumed.id}")
            return True

        async def restore_transcript_snapshot(*, append: bool = False) -> bool:
            calls.append(f"restore:{append}")
            return True

        graph = SimpleNamespace(
            resume_session=resume_session,
            restore_transcript_snapshot=restore_transcript_snapshot,
        )

        assert await SlashHandler(graph).dispatch(f"/session resume {session.id}") is True

        assert calls == [f"resume:{session.id}", "restore:True"]
        assert any(f"Resumed: {session.id}" in line and "Resume Me" in line for line in output)
    finally:
        await delete_session(session.id)


def test_session_namespace_commands_are_in_palette():
    assert ("/session list", "List saved sessions") in COMMANDS
    assert ("/session new", "Start a new session with empty context") in COMMANDS
    assert ("/session resume", "Resume a saved session") in COMMANDS


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


@pytest.mark.asyncio
async def test_title_auto_dispatches_regenerator(monkeypatch):
    output = _capture_output(monkeypatch)
    calls: list[bool] = []

    async def regenerate_session_title() -> bool:
        calls.append(True)
        return True

    graph = SimpleNamespace(
        _session=SimpleNamespace(id="session_1"),
        regenerate_session_title=regenerate_session_title,
    )

    assert await SlashHandler(graph).dispatch("/title auto") is True

    assert calls == [True]
    assert output == ["[dim]Regenerating title...[/dim]"]


@pytest.mark.asyncio
async def test_title_auto_without_user_message_prints_notice(monkeypatch):
    output = _capture_output(monkeypatch)

    async def regenerate_session_title() -> bool:
        return False

    graph = SimpleNamespace(
        _session=SimpleNamespace(id="session_1"),
        regenerate_session_title=regenerate_session_title,
    )

    assert await SlashHandler(graph).dispatch("/title auto") is True

    assert output == ["[dim]No user message available for title generation.[/dim]"]


def test_title_auto_command_is_in_palette():
    assert ("/title auto", "Regenerate session title") in COMMANDS


def test_quit_command_is_in_palette():
    assert ("/quit", "Exit voidx") in COMMANDS


def test_slash_host_adapter_forwards_guidance_source():
    calls = []
    raw = SimpleNamespace(
        submit_guidance=lambda text, **kwargs: calls.append((text, kwargs)) or True,
    )

    assert SlashHostAdapter(raw).submit_guidance("change approach", source="guard") is True

    assert calls == [("change approach", {"source": "guard"})]
