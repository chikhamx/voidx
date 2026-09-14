from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


from voidx.presentation.slash import SlashHandler
from tests.test_slash.context import command_context
import voidx.persistence.sqlite as store
from voidx.agent.adapters.persistence.session_repository import MessageRow, create_session, delete_session, get_session, save_message
from voidx.presentation.commands import COMMANDS
from voidx.presentation.output.diff import make_file_diff
from voidx.presentation.session import session_tracker
from tests.presentation_ui import make_presentation_ui

runtime_ui_port = make_presentation_ui()


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


def _graph(app=None, workspace="."):
    return SimpleNamespace(app=app, _ui=runtime_ui_port, workspace=workspace)


def _capture_output(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "voidx.presentation.slash.handler.ui.print",
        lambda text="": output.append(str(text)),
    )
    monkeypatch.setattr(
        "voidx.presentation.slash.handler.ui.error",
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
        await store.execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Old Session", (now - timedelta(days=30)).isoformat(), old_session.id),
        )
        await store.execute_commit(
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
        await store.execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Old Session", "2000-01-01T00:00:00+00:00", old_session.id),
        )
        await store.execute_commit(
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
        await store.execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Old Session", "2000-01-01T00:00:00+00:00", old_session.id),
        )
        await store.execute_commit(
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
        await store.execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Old Session", "2000-01-01T00:00:00+00:00", old_session.id),
        )
        await store.execute_commit(
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
async def test_session_list_alias_lists_savedsessions(monkeypatch, isolated_memory_store):
    output = _capture_output(monkeypatch)
    ws = "/target/ws"
    other_ws = "/other/ws"
    session = await create_session(workspace=ws)
    other_session = await create_session(workspace=other_ws)
    try:
        await store.execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Listed Session", "2026-06-15T00:00:00+00:00", session.id),
        )
        await store.execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Other Session", "2026-06-16T00:00:00+00:00", other_session.id),
        )

        assert await SlashHandler(_graph(workspace=ws)).dispatch("/session list") is True

        assert any("Sessions:" in line for line in output)
        assert any(session.id[:8] in line and "Listed Session" in line for line in output)
        assert not any(other_session.id[:8] in line for line in output)
    finally:
        await delete_session(session.id)
        await delete_session(other_session.id)


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

    graph = command_context(
        clear_current_session=clear_current_session,
        show_startup=show_startup,
    )

    assert await SlashHandler(graph).dispatch("/session new") is True

    assert calls == ["clear", "startup:{'prefer_direct': True}"]
    assert output == []


@pytest.mark.asyncio
async def test_session_resume_alias_resumes_savedsession(monkeypatch, isolated_memory_store):
    output = _capture_output(monkeypatch)
    session = await create_session()
    calls: list[str] = []
    try:
        await store.execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            ("Resume Me", "2026-06-15T00:00:00+00:00", session.id),
        )

        async def clear_current_session() -> None:
            calls.append("clear")

        async def prepare_session_switch() -> None:
            calls.append("terminal-clear")

        async def resume_session(resumed) -> bool:
            calls.append(f"resume:{resumed.id}")
            return True

        async def restore_transcript_snapshot(*, append: bool = False) -> bool:
            calls.append(f"restore:{append}")
            return True

        async def flush_after_restore() -> None:
            calls.append("flush-after-restore")

        graph = command_context(
            clear_current_session=clear_current_session,
            prepare_session_switch=prepare_session_switch,
            resume_session=resume_session,
            restore_transcript_snapshot=restore_transcript_snapshot,
            flush_after_restore=flush_after_restore,
        )

        assert await SlashHandler(graph).dispatch(f"/session resume {session.id}") is True

        assert calls == [
            "clear",
            "terminal-clear",
            f"resume:{session.id}",
            "restore:True",
            "flush-after-restore",
        ]
        assert any(f"Resumed: {session.id}" in line and "Resume Me" in line for line in output)
    finally:
        await delete_session(session.id)



@pytest.mark.asyncio
async def test_session_resume_clears_current_session_before_loading_target(monkeypatch):
    output = _capture_output(monkeypatch)
    target = SimpleNamespace(
        id="target-session",
        title="Target session",
        workspace="/tmp/target",
        message_count=4,
    )
    calls: list[str] = []

    async def clear_current_session() -> None:
        calls.append("clear")

    async def prepare_session_switch() -> None:
        calls.append("terminal-clear")

    async def resume_session(session) -> None:
        calls.append(f"resume:{session.id}")

    async def restore_transcript_snapshot(*, append: bool = False) -> bool:
        calls.append(f"restore:{append}")
        return True

    class Repository:
        async def get_session(self, session_id: str):
            calls.append(f"get:{session_id}")
            return target

    graph = command_context(
        clear_current_session=clear_current_session,
        prepare_session_switch=prepare_session_switch,
        resume_session=resume_session,
        restore_transcript_snapshot=restore_transcript_snapshot,
    )

    assert await SlashHandler(
        graph,
        session_repository=Repository(),
    ).dispatch("/resume target-session") is True

    assert calls == [
        "get:target-session",
        "clear",
        "terminal-clear",
        "resume:target-session",
        "restore:True",
    ]
    assert any("Resumed: target-session" in line for line in output)


@pytest.mark.asyncio
async def test_session_resume_does_not_clear_same_current_session(monkeypatch):
    output = _capture_output(monkeypatch)
    current = SimpleNamespace(id="current-session")
    calls: list[str] = []

    async def clear_current_session() -> None:
        calls.append("clear")

    class Repository:
        async def get_session(self, session_id: str):
            return SimpleNamespace(
                id=session_id,
                title="Current session",
                workspace=".",
                message_count=2,
            )

    graph = command_context(
        session=current,
        clear_current_session=clear_current_session,
    )

    assert await SlashHandler(
        graph,
        session_repository=Repository(),
    ).dispatch("/resume current-session") is True

    assert calls == []
    assert any("already the current session" in line for line in output)

def test_session_namespace_commands_are_in_palette():
    assert ("/session list", "List saved sessions") in COMMANDS
    assert ("/session new", "Start a new session with empty context") in COMMANDS
    assert ("/session resume", "Resume a saved session") in COMMANDS


@pytest.mark.asyncio
async def test_guide_command_submits_pending_guidance():
    calls: list[str] = []
    graph = command_context(
        submit_guidance=lambda text: calls.append(text) or True,
    )

    assert await SlashHandler(graph).dispatch("/guide keep patch small") is True

    assert calls == ["keep patch small"]


@pytest.mark.asyncio
async def test_guide_command_without_text_prints_usage(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "voidx.presentation.slash.handler.ui.print",
        lambda text="": output.append(str(text)),
    )

    assert await SlashHandler(command_context()).dispatch("/guide") is True

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

    graph = command_context(
        session=SimpleNamespace(id="session_1"),
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

    graph = command_context(
        session=SimpleNamespace(id="session_1"),
        regenerate_session_title=regenerate_session_title,
    )

    assert await SlashHandler(graph).dispatch("/title auto") is True

    assert output == ["[dim]No user message available for title generation.[/dim]"]


def test_title_auto_command_is_in_palette():
    assert ("/title auto", "Regenerate session title") in COMMANDS


def test_quit_command_is_in_palette():
    assert ("/quit", "Exit voidx") in COMMANDS


@pytest.mark.asyncio
async def test_switch_profile_reuses_fresh_session_in_place(monkeypatch):
    from voidx.agent.adapters.persistence.session_repository import SessionInfo, update_session_profile

    updated: list[tuple[str, str, object]] = []
    new_snapshot = SimpleNamespace(profile_id="chat", revision=2)
    resolved = SimpleNamespace(snapshot=new_snapshot)

    async def fake_update(session_id: str, profile: str, **kwargs) -> None:
        updated.append((session_id, profile, kwargs["profile_snapshot"]))

    monkeypatch.setattr("voidx.agent.adapters.persistence.session_repository.update_session_profile", fake_update)

    session = SessionInfo(id="fresh", workspace=".", message_count=0)
    old_snapshot = session.profile_snapshot
    calls: list[str] = []

    class Handler(SlashHandler):
        async def _session(self, args: str) -> None:
            calls.append(args)

    graph = command_context(session=session)
    await Handler(graph)._switch_profile("chat", resolved_profile=resolved)

    assert updated == [("fresh", "chat", new_snapshot)]
    assert session.runtime_profile == "chat"
    assert session.profile_snapshot is new_snapshot
    assert session.profile_snapshot is not old_snapshot
    assert calls == []


@pytest.mark.asyncio
async def test_switch_profile_creates_new_session_when_locked(monkeypatch):
    from voidx.agent.adapters.persistence.session_repository import SessionInfo

    session = SessionInfo(id="locked", workspace=".", message_count=3)
    calls: list[str] = []

    class Handler(SlashHandler):
        async def _session(self, args: str) -> None:
            calls.append(args)

    graph = command_context(session=session)
    await Handler(graph)._switch_profile("loop")

    assert calls == ["new loop"]
    assert session.runtime_profile == "coding"


@pytest.mark.asyncio
async def test_switch_profile_creates_goal_session_when_no_session(monkeypatch, isolated_memory_store):
    """/goal on a brand-new host (no session yet) must create a goal-profile session."""
    from voidx.agent.adapters.persistence.session_repository import SessionInfo

    created: list[SessionInfo] = []
    resumed: list[SessionInfo] = []

    async def fake_create_session(*args, **kwargs) -> SessionInfo:
        session = SessionInfo(
            id="goal-new",
            workspace=str(kwargs.get("workspace") or "."),
            runtime_profile=str(kwargs.get("profile") or "coding"),
        )
        created.append(session)
        return session

    async def fake_resume_session(session: SessionInfo) -> None:
        resumed.append(session)

    monkeypatch.setattr("voidx.agent.adapters.persistence.session_repository.create_session", fake_create_session)
    monkeypatch.setattr("voidx.agent.adapters.persistence.session_repository.get_session", lambda _sid: None)

    output = _capture_output(monkeypatch)
    async def fake_show_startup(**kwargs) -> bool:
        return True

    graph = command_context(
        session=None,
        resume_session=fake_resume_session,
        show_startup=fake_show_startup,
    )

    await SlashHandler(graph)._switch_profile("goal")

    assert len(created) == 1
    assert created[0].runtime_profile == "goal"
    assert resumed == created
    assert not output


@pytest.mark.asyncio
async def test_resume_lists_only_current_workspace_sessions(monkeypatch, isolated_memory_store):
    """/resume with no id lists only sessions from current workspace by updated_at desc."""
    current = "/current"
    other = "/other"

    s_other_new = await create_session(workspace=other, title="other-new")
    s_cur_old = await create_session(workspace=current, title="cur-old")
    s_cur_new = await create_session(workspace=current, title="cur-new")

    await store.execute_commit(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", s_cur_old.id),
    )
    await store.execute_commit(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        ("2026-03-01T00:00:00+00:00", s_cur_new.id),
    )
    await store.execute_commit(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        ("2026-02-01T00:00:00+00:00", s_other_new.id),
    )

    captured: list[str] = []

    class ListApp:
        async def ask_choice(self, prompt, choices):
            captured.extend(choices)
            return None

    graph = command_context(workspace=current, app=ListApp())
    _capture_output(monkeypatch)

    await SlashHandler(graph).dispatch("/resume")

    assert len(captured) == 2
    ids_in_order = [item[0].split(" | ")[0] for item in captured]
    assert ids_in_order == [s_cur_new.id[:8], s_cur_old.id[:8]]


@pytest.mark.asyncio
async def test_continue_command_runs_turn_without_persisting_user_input(monkeypatch):
    from voidx.agent.adapters.persistence.session_repository import SessionInfo
    from voidx.llm.message_markers import DEFAULT_CONTINUATION_TEXT

    calls: list[dict] = []

    async def fake_run_coding_turn(text: str, *, display_text: str | None = None, persist_user_input: bool = True, continuation: bool = False) -> None:
        calls.append({
            "text": text,
            "display_text": display_text,
            "persist_user_input": persist_user_input,
            "continuation": continuation,
        })

    session = SessionInfo(id="sess-1", workspace=".", message_count=3)
    graph = command_context(
        session=session,
        run_coding_turn=fake_run_coding_turn,
    )

    dispatched = await SlashHandler(graph).dispatch("/continue")
    assert dispatched is True
    assert len(calls) == 1
    assert calls[0]["text"] == ""
    assert calls[0]["continuation"] is True
    assert calls[0]["display_text"] == "/continue"
    assert calls[0]["persist_user_input"] is False


@pytest.mark.asyncio
async def test_continue_command_when_empty_session_prints_notice(monkeypatch):
    from voidx.agent.adapters.persistence.session_repository import SessionInfo

    output = _capture_output(monkeypatch)
    calls: list[dict] = []

    async def fake_run_coding_turn(*args, **kwargs) -> None:
        calls.append(kwargs)

    session = SessionInfo(id="sess-empty", workspace=".", message_count=0)
    graph = command_context(
        session=session,
        run_coding_turn=fake_run_coding_turn,
    )

    dispatched = await SlashHandler(graph).dispatch("/continue")
    assert dispatched is True
    assert calls == []
    assert output == ["[dim]No conversation to continue.[/dim]"]


@pytest.mark.asyncio
async def test_continue_command_when_zero_count_in_memory_but_repo_has_messages():
    from voidx.agent.adapters.persistence.session_repository import SessionInfo
    from voidx.llm.message_markers import DEFAULT_CONTINUATION_TEXT

    calls: list[dict] = []

    async def fake_run_coding_turn(text: str, *, display_text: str | None = None, persist_user_input: bool = True, continuation: bool = False) -> None:
        calls.append({
            "text": text,
            "display_text": display_text,
            "persist_user_input": persist_user_input,
            "continuation": continuation,
        })

    async def fake_count_messages(s_id: str) -> int:
        return 2

    session = SessionInfo(id="sess-stale", workspace=".", message_count=0)
    fake_repo = SimpleNamespace(
        count_messages=fake_count_messages,
    )
    graph = command_context(
        session=session,
        run_coding_turn=fake_run_coding_turn,
    )

    dispatched = await SlashHandler(graph, session_repository=fake_repo).dispatch("/continue")
    assert dispatched is True
    assert len(calls) == 1
    assert calls[0]["text"] == ""
    assert calls[0]["continuation"] is True
    assert calls[0]["display_text"] == "/continue"


@pytest.mark.asyncio
async def test_continue_command_when_zero_count_in_memory_but_repo_has_persisted_session():
    from voidx.agent.adapters.persistence.session_repository import SessionInfo

    calls: list[dict] = []

    async def fake_run_coding_turn(text: str, *, display_text: str | None = None, persist_user_input: bool = True, continuation: bool = False) -> None:
        calls.append({
            "text": text,
            "display_text": display_text,
            "persist_user_input": persist_user_input,
            "continuation": continuation,
        })

    persisted_session = SessionInfo(id="sess-stale-2", workspace=".", message_count=5)

    async def fake_get_session(s_id: str):
        return persisted_session

    session = SessionInfo(id="sess-stale-2", workspace=".", message_count=0)
    fake_repo = SimpleNamespace(
        get_session=fake_get_session,
    )
    graph = command_context(
        session=session,
        run_coding_turn=fake_run_coding_turn,
    )

    dispatched = await SlashHandler(graph, session_repository=fake_repo).dispatch("/continue")
    assert dispatched is True
    assert len(calls) == 1
    assert calls[0]["text"] == ""
    assert calls[0]["continuation"] is True


@pytest.mark.asyncio
async def test_continue_command_when_zero_count_in_memory_but_repo_has_load_messages():
    from voidx.agent.adapters.persistence.session_repository import SessionInfo

    calls: list[dict] = []

    async def fake_run_coding_turn(text: str, *, display_text: str | None = None, persist_user_input: bool = True, continuation: bool = False) -> None:
        calls.append({
            "text": text,
            "display_text": display_text,
            "persist_user_input": persist_user_input,
            "continuation": continuation,
        })

    async def fake_load_messages(s_id: str):
        return ["msg1", "msg2"]

    session = SessionInfo(id="sess-stale-3", workspace=".", message_count=0)
    fake_repo = SimpleNamespace(
        load_messages=fake_load_messages,
    )
    graph = command_context(
        session=session,
        run_coding_turn=fake_run_coding_turn,
    )

    dispatched = await SlashHandler(graph, session_repository=fake_repo).dispatch("/continue")
    assert dispatched is True
    assert len(calls) == 1
    assert calls[0]["text"] == ""
    assert calls[0]["continuation"] is True


def test_continue_command_is_in_palette():
    assert ("/continue", "Continue conversation without adding a user message") in COMMANDS
