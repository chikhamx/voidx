"""Tests for session CRUD, schema, and commit operations."""

import sqlite3
import sys
from pathlib import Path

from tests.test_agent.conftest import _create_legacy_db, _create_new_store_db, _session_dir, _table_columns


import pytest

import voidx.persistence.sqlite as store
import voidx.persistence.jsonl as jsonl_store

from voidx.agent.adapters.persistence.session_repository import (
    create_session,
    get_session,
    list_sessions,
    latest_session_for_workspace,
    count_messages,
    delete_session,
    save_message,
    load_messages,
    clear_messages,
    update_title,
    MessageRow,
)
from voidx.agent.adapters.persistence.session_cleanup import plan_session_delete
from voidx.presentation.adapters.persistence.transcript_snapshot import (
    TranscriptNodeRow,
    replace_transcript,
    load_transcript,
)
from voidx.agent.adapters.persistence.context_frame_repository import (
    build_context_frame,
    load_context_frames,
    save_context_frame,
)
from langchain_core.messages import SystemMessage, HumanMessage

@pytest.mark.asyncio
async def test_runtime_state_schema_drops_recent_user_texts_column():
    await create_session()

    columns = await _table_columns("session_runtime_state")

    assert "recent_user_texts_json" not in columns


@pytest.mark.asyncio
async def test_execute_commit_retries_transient_database_locked(monkeypatch):
    class FakeConn:
        def __init__(self):
            self.execute_calls = 0
            self.commits = 0
            self.rollbacks = 0
            self.cursor = object()

        def execute(self, sql, params=()):
            self.execute_calls += 1
            if self.execute_calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return self.cursor

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    conn = FakeConn()
    monkeypatch.setattr(store, "_get_db", lambda: conn)

    cursor = await store._execute_commit("UPDATE sessions SET title = ?", ("x",))

    assert cursor is conn.cursor
    assert conn.execute_calls == 2
    assert conn.commits == 1
    assert conn.rollbacks == 1


@pytest.mark.asyncio
async def test_execute_commit_rolls_back_before_retrying_locked_commit(monkeypatch):
    class FakeConn:
        def __init__(self):
            self.execute_calls = 0
            self.commit_calls = 0
            self.rollbacks = 0
            self.cursor = object()

        def execute(self, sql, params=()):
            self.execute_calls += 1
            return self.cursor

        def commit(self):
            self.commit_calls += 1
            if self.commit_calls == 1:
                raise sqlite3.OperationalError("database is locked")

        def rollback(self):
            self.rollbacks += 1

    conn = FakeConn()
    monkeypatch.setattr(store, "_get_db", lambda: conn)

    cursor = await store._execute_commit("UPDATE sessions SET title = ?", ("x",))

    assert cursor is conn.cursor
    assert conn.execute_calls == 2
    assert conn.commit_calls == 2
    assert conn.rollbacks == 1


def _create_legacy_db(path: Path, session_id: str, *, title: str = "Legacy") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """CREATE TABLE sessions (
                   id TEXT PRIMARY KEY,
                   title TEXT NOT NULL DEFAULT 'New session',
                   workspace TEXT NOT NULL DEFAULT '.',
                   model_provider TEXT NOT NULL DEFAULT 'anthropic',
                   model_name TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE messages (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   session_id TEXT NOT NULL,
                   role TEXT NOT NULL,
                   content TEXT NOT NULL DEFAULT '',
                   tool_calls TEXT,
                   tool_call_id TEXT,
                   created_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO sessions (
                   id, title, workspace, model_provider, model_name, created_at, updated_at
               )
               VALUES (?, ?, '.', 'anthropic', 'claude-sonnet-4-6', '2026-06-14T00:00:00+00:00', '2026-06-14T00:00:00+00:00')""",
            (session_id, title),
        )
        conn.execute(
            """INSERT INTO messages (session_id, role, content, created_at)
               VALUES (?, 'user', 'hello', '2026-06-14T00:00:00+00:00')""",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _create_new_store_db(path: Path, session_id: str, *, title: str = "New Store") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        store._init_schema(conn)
        conn.execute(
            """INSERT INTO sessions (
                   id, title, workspace, model_provider, model_name, created_at, updated_at, message_count
               )
               VALUES (?, ?, '.', 'anthropic', 'claude-sonnet-4-6', '2026-06-14T00:00:00+00:00', '2026-06-14T00:00:00+00:00', 0)""",
            (session_id, title),
        )
        conn.commit()
    finally:
        conn.close()





@pytest.mark.asyncio
async def test_legacy_root_db_is_ignored_when_store_db_missing():
    old_path = store.DATA_DIR / "voidx.db"
    new_path = store.DATA_DIR / "store" / "voidx.db"
    _create_legacy_db(old_path, "legacy-session")

    loaded = await get_session("legacy-session")

    assert loaded is None
    assert new_path.exists()
    assert old_path.exists()
    backup_path = old_path.with_suffix(old_path.suffix + ".bak")
    assert not backup_path.exists()


@pytest.mark.asyncio
async def test_new_store_db_wins_when_legacy_db_also_exists():
    old_path = store.DATA_DIR / "voidx.db"
    new_path = store.DATA_DIR / "store" / "voidx.db"
    _create_legacy_db(old_path, "legacy-session", title="Legacy")
    _create_new_store_db(new_path, "new-session", title="New Store")

    assert await get_session("legacy-session") is None
    loaded = await get_session("new-session")

    assert loaded is not None
    assert loaded.title == "New Store"
    assert old_path.exists()


@pytest.mark.asyncio
async def test_corrupt_legacy_root_db_is_ignored():
    old_path = store.DATA_DIR / "voidx.db"
    new_path = store.DATA_DIR / "store" / "voidx.db"
    tmp_path = store.DATA_DIR / "store" / "voidx.db.tmp"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b"not sqlite")

    loaded = await get_session("legacy-session")

    assert loaded is None
    assert old_path.exists()
    assert new_path.exists()
    assert not tmp_path.exists()


@pytest.mark.asyncio
async def test_session_delete_dry_run_plans_candidates_and_disk_usage():
    old_session = await create_session()
    recent_session = await create_session()
    empty_old_session = await create_session()
    try:
        await save_message(MessageRow(session_id=old_session.id, role="user", content="old"))
        session_path = _session_dir(old_session.id)
        (session_path / "artifact.txt").write_text("x" * 10, encoding="utf-8")
        await store._execute_commit(
            "UPDATE sessions SET updated_at = ? WHERE id IN (?, ?)",
            ("2026-01-01T00:00:00+00:00", old_session.id, empty_old_session.id),
        )
        await store._execute_commit(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            ("2026-06-14T00:00:00+00:00", recent_session.id),
        )

        plan = await plan_session_delete("7d", now="2026-06-15T00:00:00+00:00")

        assert {candidate.session_id for candidate in plan.candidates} == {
            empty_old_session.id,
            old_session.id,
        }
        assert plan.total_sessions == 2
        assert plan.empty_sessions == 1
        assert plan.sessions_with_messages == 1
        old_candidate = next(candidate for candidate in plan.candidates if candidate.session_id == old_session.id)
        assert old_candidate.file_bytes_to_reclaim >= 10
        assert old_candidate.bytes_to_reclaim == old_candidate.file_bytes_to_reclaim
    finally:
        await delete_session(old_session.id)
        await delete_session(recent_session.id)
        await delete_session(empty_old_session.id)
@pytest.mark.asyncio
async def test_create_and_get():
    session = await create_session(workspace="/tmp/test")
    assert session.id
    assert session.title == "New session"

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.id == session.id
    assert loaded.workspace == "/tmp/test"

    await delete_session(session.id)
@pytest.mark.asyncio
async def test_list_sessions():
    s1 = await create_session()
    s2 = await create_session()
    sessions = await list_sessions()
    ids = [s.id for s in sessions]
    assert s1.id in ids
    assert s2.id in ids
    await delete_session(s1.id)
    await delete_session(s2.id)


@pytest.mark.asyncio
async def test_latest_session_for_workspace_returns_newest_matching_workspace(tmp_path):
    workspace = str(tmp_path)
    other_workspace = str(tmp_path / "other")
    older = await create_session(workspace=workspace)
    latest = await create_session(workspace=workspace)
    other = await create_session(workspace=other_workspace)
    await update_title(latest.id, "Latest")
    try:
        loaded = await latest_session_for_workspace(workspace)

        assert loaded is not None
        assert loaded.id == latest.id
        assert loaded.workspace == workspace
    finally:
        await delete_session(older.id)
        await delete_session(latest.id)
        await delete_session(other.id)


@pytest.mark.asyncio
async def test_clear_messages():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="test"))
        await save_context_frame(build_context_frame(
            session_id=session.id,
            user_message_id=message_id,
            provider="mimo",
            model="mimo-v2.5",
            messages=[SystemMessage(content="VOIDX_RUNTIME_CONTEXT"), HumanMessage(content="test")],
        ))

        await clear_messages(session.id)

        msgs = await load_messages(session.id)
        frames = await load_context_frames(session.id)
        assert len(msgs) == 0
        assert frames == []
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_update_title():
    session = await create_session()
    await update_title(session.id, "Custom Title")
    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "Custom Title"
    await delete_session(session.id)


@pytest.mark.asyncio
async def test_delete_session_cascades():
    session = await create_session()
    await save_message(MessageRow(session_id=session.id, role="user", content="x"))
    assert _session_dir(session.id).exists()
    assert session.id in jsonl_store._session_locks
    await replace_transcript(
        session.id,
        [
            TranscriptNodeRow(
                session_id=session.id,
                turn_id=0,
                node_id=0,
                sort_order=0,
                node_type="turn",
                header="hello",
            )
        ],
        turn_count=1,
    )
    await delete_session(session.id)

    msgs = await load_messages(session.id)
    assert len(msgs) == 0
    assert await load_transcript(session.id) == []
    assert await get_session(session.id) is None
    assert not _session_dir(session.id).exists()
    assert session.id not in jsonl_store._session_locks


@pytest.mark.asyncio
async def test_delete_session_removes_file_history_directory():
    session = await create_session()
    history_dir = _session_dir(session.id) / "file-history"
    history_dir.mkdir(parents=True)
    (history_dir / "manifest.jsonl").write_text('{"path":"app.py"}\n', encoding="utf-8")
    (history_dir / "abc@v1").write_text("old\n", encoding="utf-8")

    await delete_session(session.id)

    assert not _session_dir(session.id).exists()


@pytest.mark.asyncio
async def test_create_session_persists_directory():
    info = await create_session(directory="Frameworks")
    fetched = await get_session(info.id)
    assert fetched is not None
    assert fetched.directory == "Frameworks"


@pytest.mark.asyncio
async def test_create_session_defaults_directory_to_empty():
    info = await create_session()
    fetched = await get_session(info.id)
    assert fetched is not None
    assert fetched.directory == ""


@pytest.mark.asyncio
async def test_list_sessions_returns_directory():
    await create_session(directory="opt")
    await create_session(directory="")
    sessions = await list_sessions()
    dirs = {s.directory for s in sessions}
    assert "opt" in dirs
    assert "" in dirs


@pytest.mark.asyncio
async def test_fork_session_copies_directory():
    from voidx.agent.adapters.persistence.session_repository import fork_session
    original = await create_session(directory="Downloads")
    forked = await fork_session(original.id)
    assert forked is not None
    assert forked.directory == "Downloads"
    fetched = await get_session(forked.id)
    assert fetched is not None
    assert fetched.directory == "Downloads"


@pytest.mark.asyncio
async def test_latest_session_for_workspace_returns_directory():
    await create_session(directory=".claude")
    latest = await latest_session_for_workspace(".")
    assert latest is not None
    assert latest.directory == ".claude"
