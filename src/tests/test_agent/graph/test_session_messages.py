"""Tests for session message storage, loading, and deletion."""

import json
import sys
from pathlib import Path

from tests.test_agent.conftest import _read_jsonl, _session_dir, _table_names


import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import voidx.memory.store as store
import voidx.memory.jsonl_store as jsonl_store

from voidx.agent.message_rows import message_from_row, messages_from_rows, messages_from_rows_incremental, row_fingerprint
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.runtime.task_state import (
    GoalSpec,
    TaskState,
    TodoRunState,
    TurnExchange,
    WorkflowRoute,
)
from voidx.memory.session import (
    create_session,
    get_session,
    delete_session,
    save_message,
    load_messages,
    delete_messages_from,
    delete_messages_through,
    clear_messages,
    last_messages,
    count_messages,
    MessageRow,
)
from voidx.memory.context_frames import (
    build_context_frame,
    load_context_frames,
    save_context_frame,
)
from voidx.memory.runtime_state import RuntimeStateSnapshot, save_runtime_state, load_runtime_state
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus

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
async def test_tool_message_status_round_trips_through_jsonl():
    session = await create_session()
    try:
        error_id = await save_message(MessageRow(
            session_id=session.id,
            role="tool",
            content="tool failed",
            tool_call_id="call_error",
            status="error",
        ))
        success_id = await save_message(MessageRow(
            session_id=session.id,
            role="tool",
            content="tool ok",
            tool_call_id="call_success",
            status="success",
        ))

        rows = _read_jsonl(_session_dir(session.id) / "messages.jsonl")
        error_record = next(row for row in rows if row["id"] == error_id)
        success_record = next(row for row in rows if row["id"] == success_id)
        assert error_record["status"] == "error"
        assert "status" not in success_record

        loaded = await load_messages(session.id)
        assert loaded[0].status == "error"
        assert loaded[1].status is None

        error_message = message_from_row(loaded[0])
        success_message = message_from_row(loaded[1])
        assert isinstance(error_message, ToolMessage)
        assert isinstance(success_message, ToolMessage)
        assert error_message.status == "error"
        assert success_message.status == "success"
    finally:
        await delete_session(session.id)


def test_row_fingerprint_includes_tool_status():
    base = MessageRow(
        id=1,
        session_id="s1",
        role="tool",
        content="same",
        tool_call_id="call_1",
        status="success",
    )
    changed = base.model_copy(update={"status": "error"})

    assert row_fingerprint(base) != row_fingerprint(changed)


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
                current_goal=GoalSpec(desc="keep runtime"),
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
