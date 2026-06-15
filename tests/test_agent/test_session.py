"""Tests for session persistence layer."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import voidx.memory.store as store
import voidx.memory.jsonl_store as jsonl_store

from voidx.agent.message_rows import messages_from_rows, messages_from_rows_incremental
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import (
    GoalSpec,
    GoalType,
    TaskState,
    TodoRunState,
    WorkflowRoute,
)
from voidx.memory.context_frames import (
    build_context_frame,
    load_context_frames,
    save_context_frame,
)
from voidx.memory.cleanup import plan_session_delete
from voidx.memory.jsonl_store import append_session_record
from voidx.memory.runtime_state import (
    MessageRuntimeSnapshot,
    RuntimeStateSnapshot,
    clear_runtime_state,
    load_message_runtime_snapshot,
    load_runtime_state,
    save_message_runtime_snapshot,
    save_runtime_state,
)
from voidx.memory.session import (
    create_session,
    get_session,
    list_sessions,
    latest_session_for_workspace,
    count_messages,
    delete_session,
    delete_messages_from,
    delete_messages_through,
    save_message,
    load_messages,
    clear_messages,
    update_title,
    last_messages,
    MessageRow,
)
from voidx.memory.transcript import (
    TranscriptNodeRow,
    append_transcript_summary,
    load_transcript,
    replace_transcript,
)
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus


@pytest.fixture(autouse=True)
def isolated_memory_store(tmp_path):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _session_dir(session_id: str) -> Path:
    return store.DATA_DIR / "sessions" / session_id


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _table_names() -> set[str]:
    rows = await store._fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row["name"]) for row in rows}


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
async def test_save_and_load_messages():
    session = await create_session()

    await save_message(MessageRow(session_id=session.id, role="user", content="hello"))
    await save_message(MessageRow(session_id=session.id, role="assistant", content="hi there"))
    await save_message(MessageRow(
        session_id=session.id, role="assistant", content="ok",
        tool_calls=[{"name": "read", "args": {"file_path": "x.txt"}, "id": "c1"}],
    ))

    msgs = await load_messages(session.id)
    assert len(msgs) == 3
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello"
    assert msgs[1].role == "assistant"
    assert msgs[1].content == "hi there"
    assert msgs[2].tool_calls is not None
    assert msgs[2].tool_calls[0]["name"] == "read"

    await delete_session(session.id)


@pytest.mark.asyncio
async def test_save_message_dual_writes_jsonl_and_message_count_index():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(
            session_id=session.id,
            role="assistant",
            content='[{"type":"text","text":"hi"}]',
            content_format="structured",
            tool_calls=[{"name": "read", "args": {"file_path": "x.txt"}, "id": "c1"}],
        ))

        loaded = await get_session(session.id)
        assert loaded is not None
        assert loaded.message_count == 1

        rows = _read_jsonl(_session_dir(session.id) / "messages.jsonl")
        assert rows == [{
            "type": "message",
            "id": message_id,
            "role": "assistant",
            "content": '[{"type":"text","text":"hi"}]',
            "content_format": "structured",
            "tool_calls": [{"name": "read", "args": {"file_path": "x.txt"}, "id": "c1"}],
            "created_at": rows[0]["created_at"],
        }]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_delete_messages_from_writes_jsonl_tombstone_and_updates_message_count():
    session = await create_session()
    try:
        first_id = await save_message(MessageRow(session_id=session.id, role="user", content="one"))
        second_id = await save_message(MessageRow(session_id=session.id, role="assistant", content="two"))
        await save_message(MessageRow(session_id=session.id, role="tool", content="three", tool_call_id="c1"))

        await delete_messages_from(session.id, second_id)

        loaded = await get_session(session.id)
        assert loaded is not None
        assert loaded.message_count == 1
        assert [row.id for row in await load_messages(session.id)] == [first_id]

        rows = _read_jsonl(_session_dir(session.id) / "messages.jsonl")
        assert rows[-1]["type"] == "message_deleted"
        assert rows[-1]["mode"] == "from"
        assert rows[-1]["first_message_id"] == second_id
        assert rows[-1]["reason"] == "delete_messages_from"

        context_deletes = _read_jsonl(_session_dir(session.id) / "context" / "deletes.jsonl")
        assert context_deletes[-1]["type"] == "context_frame_deleted"
        assert context_deletes[-1]["mode"] == "from"
        assert context_deletes[-1]["first_user_message_id"] == second_id
        assert context_deletes[-1]["reason"] == "delete_messages_from"

        runtime_deletes = _read_jsonl(_session_dir(session.id) / "runtime.jsonl")
        assert runtime_deletes[-1]["type"] == "runtime_state_deleted"
        assert runtime_deletes[-1]["mode"] == "from"
        assert runtime_deletes[-1]["first_message_id"] == second_id
        assert runtime_deletes[-1]["reason"] == "delete_messages_from"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_delete_messages_through_writes_jsonl_tombstone_and_updates_message_count():
    session = await create_session()
    try:
        first_id = await save_message(MessageRow(session_id=session.id, role="user", content="one"))
        second_id = await save_message(MessageRow(session_id=session.id, role="assistant", content="two"))
        third_id = await save_message(MessageRow(session_id=session.id, role="tool", content="three", tool_call_id="c1"))

        await delete_messages_through(session.id, second_id)

        loaded = await get_session(session.id)
        assert loaded is not None
        assert loaded.message_count == 1
        assert [row.id for row in await load_messages(session.id)] == [third_id]

        rows = _read_jsonl(_session_dir(session.id) / "messages.jsonl")
        assert rows[0]["id"] == first_id
        assert rows[-1]["type"] == "message_deleted"
        assert rows[-1]["mode"] == "through"
        assert rows[-1]["last_message_id"] == second_id
        assert rows[-1]["reason"] == "delete_messages_through"

        context_deletes = _read_jsonl(_session_dir(session.id) / "context" / "deletes.jsonl")
        assert context_deletes[-1]["type"] == "context_frame_deleted"
        assert context_deletes[-1]["mode"] == "through"
        assert context_deletes[-1]["last_user_message_id"] == second_id
        assert context_deletes[-1]["reason"] == "delete_messages_through"

        runtime_deletes = _read_jsonl(_session_dir(session.id) / "runtime.jsonl")
        assert runtime_deletes[-1]["type"] == "runtime_state_deleted"
        assert runtime_deletes[-1]["mode"] == "through"
        assert runtime_deletes[-1]["last_message_id"] == second_id
        assert runtime_deletes[-1]["reason"] == "delete_messages_through"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_save_message_after_delete_messages_through_keeps_monotonic_ids_and_order():
    session = await create_session()
    try:
        ids = [
            await save_message(MessageRow(session_id=session.id, role="user", content=str(index)))
            for index in range(1, 6)
        ]
        await delete_messages_through(session.id, ids[2])

        new_id = await save_message(MessageRow(session_id=session.id, role="user", content="new"))
        messages = await load_messages(session.id)

        assert new_id > ids[-1]
        assert [message.id for message in messages] == [ids[3], ids[4], new_id]
        assert [message.content for message in messages] == ["4", "5", "new"]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_delete_messages_through_keeps_latest_session_runtime_state():
    session = await create_session()
    try:
        first_id = await save_message(MessageRow(session_id=session.id, role="user", content="one"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="two"))
        await save_runtime_state(session.id, RuntimeStateSnapshot(
            interaction_mode=InteractionMode.GOAL,
            task_state=TaskState(
                current_intent=TaskIntent.CODING,
                current_goal=GoalSpec(type=GoalType.FEATURE, desc="keep runtime"),
            ),
            compaction_summary="summary",
            session_time="session-time",
        ))

        await delete_messages_through(session.id, first_id)

        loaded = await load_runtime_state(session.id)
        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.compaction_summary == "summary"
        assert loaded.session_time == "session-time"
        assert loaded.task_state.current_goal is not None
        assert loaded.task_state.current_goal.desc == "keep runtime"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_messages_replays_jsonl_after_message_delete_tombstone():
    session = await create_session()
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="one"))
        second_id = await save_message(MessageRow(session_id=session.id, role="assistant", content="two"))
        third_id = await save_message(MessageRow(session_id=session.id, role="tool", content="three", tool_call_id="c1"))
        await delete_messages_through(session.id, second_id)

        messages = await load_messages(session.id)

        assert len(messages) == 1
        assert messages[0].id == third_id
        assert messages[0].role == "tool"
        assert messages[0].content == "three"
        assert messages[0].tool_call_id == "c1"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_save_message_does_not_create_legacy_messages_table():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="hello"))

        tables = await _table_names()
        loaded = await load_messages(session.id)

        assert "messages" not in tables
        assert [message.id for message in loaded] == [message_id]
        assert loaded[0].content == "hello"
        assert await count_messages(session.id) == 1
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_last_messages_reads_from_jsonl_payload():
    session = await create_session()
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="one"))
        second_id = await save_message(MessageRow(session_id=session.id, role="assistant", content="two"))
        third_id = await save_message(MessageRow(session_id=session.id, role="tool", content="three", tool_call_id="c1"))

        messages = await last_messages(session.id, 2)

        assert [message.id for message in messages] == [second_id, third_id]
        assert [message.content for message in messages] == ["two", "three"]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_messages_replays_jsonl_after_session_cleared_marker():
    session = await create_session()
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old"))
        await clear_messages(session.id)
        new_id = await save_message(MessageRow(session_id=session.id, role="user", content="new"))

        messages = await load_messages(session.id)

        assert len(messages) == 1
        assert messages[0].id == new_id
        assert messages[0].content == "new"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_messages_skips_corrupt_jsonl_lines():
    session = await create_session()
    try:
        first_id = await save_message(MessageRow(session_id=session.id, role="user", content="one"))
        second_id = await save_message(MessageRow(session_id=session.id, role="assistant", content="two"))
        path = _session_dir(session.id) / "messages.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write("{not json}\n")
        messages = await load_messages(session.id)

        assert [message.id for message in messages] == [first_id, second_id]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_messages_uses_jsonl_even_when_message_count_is_stale():
    session = await create_session()
    try:
        first_id = await save_message(MessageRow(session_id=session.id, role="user", content="one"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="two"))
        path = _session_dir(session.id) / "messages.jsonl"
        rows = _read_jsonl(path)
        path.write_text(json.dumps(rows[0], ensure_ascii=False) + "\n", encoding="utf-8")

        messages = await load_messages(session.id)

        assert [message.id for message in messages] == [first_id]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_clear_messages_writes_jsonl_reset_and_resets_message_count():
    session = await create_session()
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="one"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="two"))

        await clear_messages(session.id)

        loaded = await get_session(session.id)
        assert loaded is not None
        assert loaded.message_count == 0
        assert await load_messages(session.id) == []

        rows = _read_jsonl(_session_dir(session.id) / "messages.jsonl")
        assert rows[-1]["type"] == "session_cleared"
        assert rows[-1]["reason"] == "clear_messages"
        assert rows[-1]["previous_message_count"] == 2

        context_deletes = _read_jsonl(_session_dir(session.id) / "context" / "deletes.jsonl")
        assert context_deletes[-1]["type"] == "context_frame_deleted"
        assert context_deletes[-1]["mode"] == "all"
        assert context_deletes[-1]["reason"] == "clear_messages"

        runtime_deletes = _read_jsonl(_session_dir(session.id) / "runtime.jsonl")
        assert runtime_deletes[-1]["type"] == "runtime_state_deleted"
        assert runtime_deletes[-1]["mode"] == "all"
        assert runtime_deletes[-1]["reason"] == "clear_messages"

        transcript_records = _read_jsonl(_session_dir(session.id) / "transcript.jsonl")
        assert transcript_records[-1]["type"] == "transcript_reset"
        assert transcript_records[-1]["reason"] == "clear_messages"
    finally:
        await delete_session(session.id)


def test_messages_from_rows_preserves_content_and_tool_fields():
    rows = [
        MessageRow(id=1, session_id="s", role="system", content="system"),
        MessageRow(
            id=2,
            session_id="s",
            role="user",
            content='[{"type":"text","text":"hello"}]',
            content_format="structured",
        ),
        MessageRow(
            id=3,
            session_id="s",
            role="assistant",
            content="ok",
            tool_calls=[{"name": "read", "args": {"file_path": "x.txt"}, "id": "c1"}],
        ),
        MessageRow(id=4, session_id="s", role="tool", content="result", tool_call_id="c1"),
    ]

    messages = messages_from_rows(rows)

    assert isinstance(messages[0], SystemMessage)
    assert messages[0].id == "1"
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content == [{"type": "text", "text": "hello"}]
    assert isinstance(messages[2], AIMessage)
    assert messages[2].tool_calls[0]["name"] == "read"
    assert isinstance(messages[3], ToolMessage)
    assert messages[3].tool_call_id == "c1"


def test_messages_from_rows_incremental_reuses_cached_rows():
    rows = [
        MessageRow(id=1, session_id="s", role="user", content="hello"),
        MessageRow(id=2, session_id="s", role="assistant", content="hi"),
    ]

    first, cache = messages_from_rows_incremental(rows, {})
    second, next_cache = messages_from_rows_incremental(rows, cache)

    assert second[0] is first[0]
    assert second[1] is first[1]
    assert set(next_cache) == {1, 2}


def test_messages_from_rows_incremental_rebuilds_changed_row_same_id():
    first, cache = messages_from_rows_incremental([
        MessageRow(id=1, session_id="s", role="tool", content="old", tool_call_id="c1"),
    ], {})

    second, _ = messages_from_rows_incremental([
        MessageRow(id=1, session_id="s", role="tool", content="new", tool_call_id="c1"),
    ], cache)

    assert second[0] is not first[0]
    assert second[0].content == "new"


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
async def test_replace_and_load_transcript_nodes():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header="question",
                ),
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=1,
                    parent_node_id=0,
                    sort_order=1,
                    node_type="thought",
                    header="Thinking",
                    body_lines=["step 1"],
                    collapsed=True,
                    metadata={"meta": "Thinking for 2s"},
                ),
            ],
            turn_count=1,
        )

        rows = await load_transcript(session.id)

        assert [row.node_type for row in rows] == ["turn", "thought"]
        assert rows[1].parent_node_id == 0
        assert rows[1].body_lines == ["step 1"]
        assert rows[1].metadata["meta"] == "Thinking for 2s"

        transcript_records = _read_jsonl(_session_dir(session.id) / "transcript.jsonl")
        assert [record["type"] for record in transcript_records] == [
            "transcript_reset",
            "turn_start",
            "node",
            "node",
            "turn_end",
        ]
        assert "message_id" not in transcript_records[2]
        assert transcript_records[3]["metadata"]["meta"] == "Thinking for 2s"

        index = json.loads((_session_dir(session.id) / "transcript.idx.json").read_text(encoding="utf-8"))
        assert index["version"] == 1
        assert index["transcript_size"] == (_session_dir(session.id) / "transcript.jsonl").stat().st_size
        assert index["last_reset_offset"] == 0
        assert "0" in index["turn_offsets"]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_replace_transcript_does_not_create_legacy_transcript_tables():
    session = await create_session()
    try:
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

        tables = await _table_names()
        loaded = await load_transcript(session.id)

        assert "turns" not in tables
        assert "transcript_nodes" not in tables
        assert len(loaded) == 1
        assert loaded[0].header == "hello"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_replays_jsonl_records():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="question"))
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header="question",
                    message_id=message_id,
                ),
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=1,
                    parent_node_id=0,
                    sort_order=1,
                    node_type="tool_result",
                    header="Read",
                    body_lines=["line 1"],
                    status="done",
                    tool_call_id="tc_1",
                    metadata={"payload": {"path": "x.txt"}},
                ),
            ],
            turn_count=1,
        )

        rows = await load_transcript(session.id)

        assert [row.node_type for row in rows] == ["turn", "tool_result"]
        assert rows[0].message_id == message_id
        assert rows[1].parent_node_id == 0
        assert rows[1].tool_call_id == "tc_1"
        assert rows[1].metadata["payload"]["path"] == "x.txt"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_uses_index_seek_after_latest_reset():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [TranscriptNodeRow(
                session_id=session.id,
                turn_id=0,
                node_id=0,
                sort_order=0,
                node_type="turn",
                header="old",
            )],
            turn_count=1,
        )
        await replace_transcript(
            session.id,
            [TranscriptNodeRow(
                session_id=session.id,
                turn_id=0,
                node_id=0,
                sort_order=0,
                node_type="turn",
                header="latest",
            )],
            turn_count=1,
        )

        path = _session_dir(session.id) / "transcript.jsonl"
        data = path.read_bytes()
        latest_offset = json.loads((_session_dir(session.id) / "transcript.idx.json").read_text(encoding="utf-8"))[
            "last_reset_offset"
        ]
        old_offset = data.index(b"old")
        assert old_offset < latest_offset
        path.write_bytes(data[:old_offset] + b"\xff" + data[old_offset + 1:])

        rows = await load_transcript(session.id)

        assert [row.header for row in rows] == ["latest"]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_uses_checkpoint_when_prior_jsonl_is_corrupt():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header="checkpoint question",
                    body_lines=["from checkpoint"],
                ),
            ],
            turn_count=1,
        )
        index_path = _session_dir(session.id) / "transcript.idx.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        checkpoint_path = _session_dir(session.id) / index["last_checkpoint_path"]
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert index["last_checkpoint_offset"] == checkpoint["offset"]
        assert checkpoint["rows"][0]["header"] == "checkpoint question"

        path = _session_dir(session.id) / "transcript.jsonl"
        data = path.read_bytes()
        old_offset = data.index(b"checkpoint question")
        assert old_offset < index["last_checkpoint_offset"]
        path.write_bytes(data[:old_offset] + b"\xff" + data[old_offset + 1:])

        rows = await load_transcript(session.id)

        assert [row.header for row in rows] == ["checkpoint question"]
        assert rows[0].body_lines == ["from checkpoint"]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_applies_records_after_checkpoint():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=0,
                    sort_order=0,
                    node_type="thought",
                    header="Thinking",
                    body_lines=["before checkpoint"],
                    status="running",
                ),
            ],
            turn_count=1,
        )
        await append_session_record(session.id, "transcript.jsonl", {
            "type": "node_update",
            "turn_id": 0,
            "node_id": 0,
            "status": "done",
            "body_append": ["after checkpoint"],
        })
        index_path = _session_dir(session.id) / "transcript.idx.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["transcript_size"] = (_session_dir(session.id) / "transcript.jsonl").stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")
        rows = await load_transcript(session.id)

        assert len(rows) == 1
        assert rows[0].status == "done"
        assert rows[0].body_lines == ["before checkpoint", "after checkpoint"]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_applies_node_update_merge_semantics():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=0,
                    sort_order=0,
                    node_type="thought",
                    header="Thinking",
                    body_lines=["step 1"],
                    status="running",
                    metadata={"meta": "old", "payload": {"path": "a.py"}, "stale": True},
                ),
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=1,
                    sort_order=1,
                    node_type="tool_result",
                    header="Read",
                    body_lines=["line 1"],
                ),
            ],
            turn_count=1,
        )
        await append_session_record(session.id, "transcript.jsonl", {
            "type": "node_update",
            "turn_id": 0,
            "node_id": 0,
            "status": "done",
            "body_append": ["step 2"],
            "metadata": {"payload": {"path": "b.py"}, "extra": 1},
            "metadata_delete": ["stale"],
            "elapsed": 1.5,
        })
        await append_session_record(session.id, "transcript.jsonl", {
            "type": "node_update",
            "turn_id": 0,
            "node_id": 0,
            "body_lines": ["final"],
            "elapsed": None,
        })
        await append_session_record(session.id, "transcript.jsonl", {
            "type": "node_update",
            "turn_id": 0,
            "node_id": 1,
            "body_append": ["line 2"],
        })
        rows = await load_transcript(session.id)

        assert len(rows) == 2
        assert rows[0].status == "done"
        assert rows[0].body_lines == ["final"]
        assert rows[0].elapsed is None
        assert rows[0].metadata == {
            "meta": "old",
            "payload": {"path": "b.py"},
            "extra": 1,
        }
        assert rows[1].body_lines == ["line 1", "line 2"]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_uses_summary_offset_and_skips_summarized_turns():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header="old question",
                ),
            ],
            turn_count=1,
        )
        await append_transcript_summary(session.id, turn_id=0, content="older context summary")
        await append_session_record(session.id, "transcript.jsonl", {
            "type": "node",
            "turn_id": 1,
            "node_id": 1,
            "sort_order": 1,
            "node_type": "assistant",
            "header": "tail answer",
            "body_lines": ["still visible"],
            "status": "done",
        })
        index_path = _session_dir(session.id) / "transcript.idx.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["transcript_size"] = (_session_dir(session.id) / "transcript.jsonl").stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")
        path = _session_dir(session.id) / "transcript.jsonl"
        data = path.read_bytes()
        old_offset = data.index(b"old question")
        summary_offset = json.loads((_session_dir(session.id) / "transcript.idx.json").read_text(encoding="utf-8"))[
            "summary_offsets"
        ]["0"]
        assert old_offset < summary_offset
        path.write_bytes(data[:old_offset] + b"\xff" + data[old_offset + 1:])

        rows = await load_transcript(session.id)

        assert [row.node_type for row in rows] == ["summary", "assistant"]
        assert rows[0].turn_id == 0
        assert rows[0].header == "Compaction summary"
        assert rows[0].body_lines == ["older context summary"]
        assert rows[1].turn_id == 1
        assert rows[1].header == "tail answer"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_rebuilds_corrupt_index_after_fallback_scan():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header="old question",
                ),
            ],
            turn_count=1,
        )
        await append_transcript_summary(session.id, turn_id=0, content="older context summary")
        index_path = _session_dir(session.id) / "transcript.idx.json"
        index_path.write_text("{not json", encoding="utf-8")

        rows = await load_transcript(session.id)

        assert [row.node_type for row in rows] == ["summary"]
        rebuilt = json.loads(index_path.read_text(encoding="utf-8"))
        assert rebuilt["transcript_size"] == (_session_dir(session.id) / "transcript.jsonl").stat().st_size
        assert isinstance(rebuilt["last_reset_offset"], int)
        assert isinstance(rebuilt["summary_offsets"]["0"], int)

        path = _session_dir(session.id) / "transcript.jsonl"
        data = path.read_bytes()
        old_offset = data.index(b"old question")
        path.write_bytes(data[:old_offset] + b"\xff" + data[old_offset + 1:])

        rows = await load_transcript(session.id)

        assert [row.node_type for row in rows] == ["summary"]
        assert rows[0].body_lines == ["older context summary"]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_rebuilds_missing_index_after_fallback_scan():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header="question",
                ),
            ],
            turn_count=1,
        )
        index_path = _session_dir(session.id) / "transcript.idx.json"
        index_path.unlink()

        rows = await load_transcript(session.id)

        assert [row.header for row in rows] == ["question"]
        rebuilt = json.loads(index_path.read_text(encoding="utf-8"))
        assert rebuilt["transcript_size"] == (_session_dir(session.id) / "transcript.jsonl").stat().st_size
        assert rebuilt["last_reset_offset"] == 0
        assert "0" in rebuilt["turn_offsets"]
    finally:
        await delete_session(session.id)


def test_context_frame_hashes_stable_prefix_before_long_summary():
    first = build_context_frame(
        session_id="s1",
        provider="mimo",
        model="mimo-v2.5",
        messages=[
            SystemMessage(content=(
                "VOIDX_RUNTIME_CONTEXT\n\n"
                "## Base System\nbase\n\n"
                "## Role Prompt\nrole\n\n"
                "## Session Date\n2026-05-31 CST\n\n"
                "## Long Summary\n- first summary"
            )),
            HumanMessage(content="hi"),
        ],
    )
    second = build_context_frame(
        session_id="s1",
        provider="mimo",
        model="mimo-v2.5",
        messages=[
            SystemMessage(content=(
                "VOIDX_RUNTIME_CONTEXT\n\n"
                "## Base System\nbase\n\n"
                "## Role Prompt\nrole\n\n"
                "## Session Date\n2026-05-31 CST\n\n"
                "## Long Summary\n- second summary"
            )),
            HumanMessage(content="hi"),
        ],
    )

    assert first.prefix_hash == second.prefix_hash
    assert first.frame_hash != second.frame_hash


@pytest.mark.asyncio
async def test_context_frame_round_trips_compiled_messages():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="hello"))
        record = build_context_frame(
            session_id=session.id,
            user_message_id=message_id,
            frame_kind="main",
            agent_persona="voidx",
            provider="mimo",
            model="mimo-v2.5",
            token_estimate=42,
            metadata={"step": 1},
            messages=[
                SystemMessage(content="VOIDX_RUNTIME_CONTEXT\n\n## Base System\nbase"),
                HumanMessage(content="hello"),
            ],
        )

        frame_id = await save_context_frame(record)
        frames = await load_context_frames(session.id)

        assert frames[0].id == frame_id
        assert frames[0].user_message_id == message_id
        assert frames[0].agent_persona == "voidx"
        assert frames[0].token_estimate == 42
        assert frames[0].metadata["step"] == 1
        assert frames[0].messages[-1]["content"] == "hello"

        context_rows = _read_jsonl(_session_dir(session.id) / "context" / f"{frame_id}.jsonl")
        assert context_rows == record.messages
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_context_frame_stores_file_path_not_messages_json():
    session = await create_session()
    try:
        record = build_context_frame(
            session_id=session.id,
            frame_kind="main",
            agent_persona="voidx",
            provider="mimo",
            model="mimo-v2.5",
            messages=[
                SystemMessage(content="VOIDX_RUNTIME_CONTEXT\n\n## Base System\nbase"),
                HumanMessage(content="hello"),
            ],
        )

        frame_id = await save_context_frame(record)

        row = await store._fetch_one("SELECT file_path FROM context_frames WHERE id = ?", (frame_id,))
        frames = await load_context_frames(session.id)

        assert row is not None
        assert row["file_path"] == f"context/{frame_id}.jsonl"
        assert frames[0].messages[-1]["content"] == "hello"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_context_frame_loads_messages_from_jsonl_payload():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="hello"))
        record = build_context_frame(
            session_id=session.id,
            user_message_id=message_id,
            frame_kind="main",
            agent_persona="voidx",
            provider="mimo",
            model="mimo-v2.5",
            token_estimate=42,
            metadata={"step": 1},
            messages=[
                SystemMessage(content="VOIDX_RUNTIME_CONTEXT\n\n## Base System\nbase"),
                HumanMessage(content="hello"),
            ],
        )
        frame_id = await save_context_frame(record)

        frames = await load_context_frames(session.id)

        assert frames[0].id == frame_id
        assert frames[0].messages == record.messages
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_context_frame_loader_applies_delete_tombstones_to_existing_frames_only():
    session = await create_session()
    try:
        first_message_id = await save_message(MessageRow(session_id=session.id, role="user", content="old"))
        second_message_id = await save_message(MessageRow(session_id=session.id, role="user", content="new"))

        first_record = build_context_frame(
            session_id=session.id,
            user_message_id=first_message_id,
            provider="mimo",
            model="mimo-v2.5",
            messages=[HumanMessage(content="old")],
        )
        first_frame_id = await save_context_frame(first_record)

        await append_session_record(session.id, "context/deletes.jsonl", {
            "type": "context_frame_deleted",
            "mode": "from",
            "first_user_message_id": first_message_id,
            "reason": "test",
            "created_at": store._now(),
        })

        second_record = build_context_frame(
            session_id=session.id,
            user_message_id=second_message_id,
            provider="mimo",
            model="mimo-v2.5",
            messages=[HumanMessage(content="new")],
        )
        second_frame_id = await save_context_frame(second_record)

        frames = await load_context_frames(session.id)

        assert [frame.id for frame in frames] == [second_frame_id]
        assert first_frame_id != second_frame_id
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_runtime_state_round_trips_structured_goal_state():
    session = await create_session()
    try:
        state = TaskState(
            current_intent=TaskIntent.CODING,
            previous_intent=TaskIntent.CODING,
            current_goal=GoalSpec(type=GoalType.DESIGN, desc="优化 markdown 渲染截断"),
            workflow_route=WorkflowRoute(join="brainstorm", leave="plan"),
            recent_user_texts=["看看现状", "给个方案"],
            workflow_runs={
                "brainstorm": WorkflowRunState(
                    name="brainstorm",
                    status=WorkflowRunStatus.ACTIVE,
                    source=WorkflowActivationSource.WORKFLOW,
                    reason="goal:design",
                    goal_type="design",
                    scope="优化 markdown 渲染截断",
                )
            },
            todo_state=TodoRunState.model_validate({
                "summary": "0/2 done · 1 active · 1 pending",
                "items": [
                    {"content": "inspect current behavior", "status": "in_progress"},
                    {"content": "write focused test", "status": "pending"},
                ],
                "updated_at": "2026-06-11T00:00:00+00:00",
            }),
        )

        await save_runtime_state(
            session.id,
            RuntimeStateSnapshot(
                interaction_mode=InteractionMode.GOAL,
                task_state=state,
                session_time="2026-06-11 CST",
            ),
        )

        loaded = await load_runtime_state(session.id)

        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.session_time == "2026-06-11 CST"
        assert loaded.task_state.current_intent == TaskIntent.CODING
        assert loaded.task_state.previous_intent == TaskIntent.CODING
        assert loaded.task_state.current_goal is not None
        assert loaded.task_state.current_goal.type == GoalType.DESIGN
        assert loaded.task_state.current_goal.desc == "优化 markdown 渲染截断"
        assert loaded.task_state.workflow_route is not None
        assert loaded.task_state.workflow_route.join == "brainstorm"
        assert loaded.task_state.workflow_route.leave == "plan"
        assert loaded.task_state.recent_user_texts == ["看看现状", "给个方案"]
        assert loaded.task_state.todo_state is not None
        assert loaded.task_state.todo_state.summary == "0/2 done · 1 active · 1 pending"
        assert loaded.task_state.todo_state.items[0].content == "inspect current behavior"
        assert loaded.task_state.workflow_runs["brainstorm"].status == WorkflowRunStatus.ACTIVE
        assert loaded.task_state.workflow_runs["brainstorm"].goal_type == "design"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_runtime_state_empty_todo_persists_as_cleared_state():
    session = await create_session()
    try:
        await save_runtime_state(
            session.id,
            RuntimeStateSnapshot(
                interaction_mode=InteractionMode.AUTO,
                task_state=TaskState(
                    todo_state=TodoRunState.model_validate({
                        "summary": "0/0 done · 0 active · 0 pending",
                        "items": [],
                    }),
                ),
            ),
        )

        loaded = await load_runtime_state(session.id)

        assert loaded.task_state.todo_state is None
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_message_runtime_snapshot_round_trips_per_turn_state():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="给个方案"))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=message_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.CODING,
            current_goal=GoalSpec(type=GoalType.DESIGN, desc="优化 markdown 渲染截断"),
            workflow_route=WorkflowRoute(join="brainstorm", leave="plan"),
            workflow_runs={
                "brainstorm": WorkflowRunState(
                    name="brainstorm",
                    status=WorkflowRunStatus.ACTIVE,
                    reason="goal:design",
                    goal_type="design",
                )
            },
        ))

        loaded = await load_message_runtime_snapshot(message_id)

        assert loaded is not None
        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.task_intent == TaskIntent.CODING
        assert loaded.current_goal is not None
        assert loaded.current_goal.type == GoalType.DESIGN
        assert loaded.workflow_route is not None
        assert loaded.workflow_route.join == "brainstorm"
        assert loaded.workflow_route.leave == "plan"
        assert loaded.workflow_runs["brainstorm"].goal_type == "design"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_message_runtime_snapshot_round_trips_from_runtime_debug_jsonl():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="给个方案"))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=message_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.CODING,
            current_goal=GoalSpec(type=GoalType.DESIGN, desc="优化 markdown 渲染截断"),
            workflow_route=WorkflowRoute(join="brainstorm", leave="plan"),
            workflow_runs={
                "brainstorm": WorkflowRunState(
                    name="brainstorm",
                    status=WorkflowRunStatus.ACTIVE,
                    reason="goal:design",
                    goal_type="design",
                )
            },
        ))
        rows = _read_jsonl(_session_dir(session.id) / "runtime_debug.jsonl")
        loaded = await load_message_runtime_snapshot(message_id)

        assert rows[-1]["type"] == "message_runtime_snapshot"
        assert rows[-1]["message_id"] == message_id
        assert loaded is not None
        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.current_goal is not None
        assert loaded.current_goal.type == GoalType.DESIGN
        assert loaded.workflow_route is not None
        assert loaded.workflow_route.join == "brainstorm"
        assert loaded.workflow_runs["brainstorm"].goal_type == "design"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_message_runtime_snapshot_does_not_write_legacy_snapshot_table():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="给个方案"))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=message_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.CODING,
            current_goal=GoalSpec(type=GoalType.DESIGN, desc="优化 markdown 渲染截断"),
        ))

        tables = await _table_names()
        loaded = await load_message_runtime_snapshot(message_id)

        assert "message_runtime_snapshots" not in tables
        assert loaded is not None
        assert loaded.message_id == message_id
        assert loaded.current_goal is not None
        assert loaded.current_goal.type == GoalType.DESIGN
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_message_runtime_snapshot_updates_latest_session_runtime_state():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="给个方案"))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=message_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.CODING,
            current_goal=GoalSpec(type=GoalType.DESIGN, desc="优化 markdown 渲染截断"),
            workflow_route=WorkflowRoute(join="brainstorm", leave="plan"),
            workflow_runs={
                "brainstorm": WorkflowRunState(
                    name="brainstorm",
                    status=WorkflowRunStatus.ACTIVE,
                    reason="goal:design",
                    goal_type="design",
                )
            },
        ))

        loaded = await load_runtime_state(session.id)

        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.task_state.current_intent == TaskIntent.CODING
        assert loaded.task_state.current_goal is not None
        assert loaded.task_state.current_goal.type == GoalType.DESIGN
        assert loaded.task_state.workflow_route is not None
        assert loaded.task_state.workflow_route.join == "brainstorm"
        assert loaded.task_state.workflow_runs["brainstorm"].goal_type == "design"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_delete_session_cascades_all_child_tables():
    """delete_session should cascade-delete rows in ALL child tables via FK."""
    session = await create_session()
    try:
        msg_id = await save_message(MessageRow(session_id=session.id, role="user", content="x"))
        await replace_transcript(
            session.id,
            [TranscriptNodeRow(session_id=session.id, turn_id=0, node_id=0, sort_order=0, node_type="turn", header="hello")],
            turn_count=1,
        )
        await save_context_frame(build_context_frame(
            session_id=session.id,
            user_message_id=msg_id,
            provider="mimo",
            model="mimo-v2.5",
            messages=[SystemMessage(content="VOIDX_RUNTIME_CONTEXT"), HumanMessage(content="x")],
        ))
        await save_runtime_state(session.id, RuntimeStateSnapshot(
            interaction_mode=InteractionMode.GOAL,
            task_state=TaskState(current_goal=GoalSpec(type=GoalType.CHORE, desc="test goal")),
        ))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=msg_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.CODING,
        ))

        await delete_session(session.id)

        assert await load_messages(session.id) == []
        assert await load_transcript(session.id) == []
        assert await load_context_frames(session.id) == []
        assert await get_session(session.id) is None
        loaded_state = await load_runtime_state(session.id)
        assert loaded_state.interaction_mode == InteractionMode.AUTO
        assert loaded_state.task_state.current_goal is None
        assert await load_message_runtime_snapshot(msg_id) is None
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_clear_messages_cascades_runtime_state():
    """clear_messages should also clear session_runtime_state."""
    session = await create_session()
    try:
        msg_id = await save_message(MessageRow(session_id=session.id, role="user", content="test"))
        await save_runtime_state(session.id, RuntimeStateSnapshot(
            interaction_mode=InteractionMode.GOAL,
            task_state=TaskState(current_goal=GoalSpec(type=GoalType.CHORE, desc="test goal")),
        ))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=msg_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
        ))

        await clear_messages(session.id)

        msgs = await load_messages(session.id)
        assert len(msgs) == 0
        loaded_state = await load_runtime_state(session.id)
        assert loaded_state.interaction_mode == InteractionMode.AUTO
        assert loaded_state.task_state.current_goal is None
        assert await load_message_runtime_snapshot(msg_id) is None
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_clear_runtime_state_resets_structured_state():
    session = await create_session()
    try:
        await save_runtime_state(
            session.id,
            RuntimeStateSnapshot(
                interaction_mode=InteractionMode.GOAL,
                task_state=TaskState(current_goal=GoalSpec(type=GoalType.FEATURE, desc="修复 UI")),
            ),
        )

        await clear_runtime_state(session.id)
        loaded = await load_runtime_state(session.id)

        assert loaded.interaction_mode == InteractionMode.AUTO
        assert loaded.task_state.current_goal is None
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_graph_session_runtime_persists_and_restores_structured_state():
    from types import SimpleNamespace

    from voidx.agent.graph.session_runtime import GraphSessionRuntime

    session = await create_session()
    try:
        host = SimpleNamespace(
            _session=session,
            _interaction_mode=InteractionMode.GOAL,
            _task_state=TaskState(current_goal=GoalSpec(type=GoalType.CHORE, desc="ship 5B")),
            _compaction_summary="summary",
            _session_date="2026-06-11 CST",
        )

        runtime = GraphSessionRuntime(host)
        await runtime.persist_runtime_state()

        host._interaction_mode = InteractionMode.AUTO
        host._task_state = TaskState()
        host._compaction_summary = ""
        host._session_date = ""

        await runtime.restore_runtime_state()

        assert host._interaction_mode == InteractionMode.GOAL
        assert host._task_state.current_goal is not None
        assert host._task_state.current_goal.desc == "ship 5B"
        assert host._compaction_summary == "summary"
        assert host._session_date == "2026-06-11 CST"
    finally:
        await delete_session(session.id)
