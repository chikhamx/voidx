"""Tests for session persistence layer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.memory.session import (
    create_session,
    get_session,
    list_sessions,
    delete_session,
    save_message,
    load_messages,
    clear_messages,
    update_title,
    MessageRow,
)


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
async def test_clear_messages():
    session = await create_session()
    await save_message(MessageRow(session_id=session.id, role="user", content="test"))
    await clear_messages(session.id)
    msgs = await load_messages(session.id)
    assert len(msgs) == 0
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
    await delete_session(session.id)

    msgs = await load_messages(session.id)
    assert len(msgs) == 0
    assert await get_session(session.id) is None
