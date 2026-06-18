"""Tests for context frame persistence and loading."""

import json
import sys
from pathlib import Path

from tests.test_agent.conftest import _read_jsonl, _session_dir

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import voidx.memory.store as store

from voidx.memory.session import (
    create_session,
    get_session,
    delete_session,
    save_message,
    load_messages,
    delete_messages_from,
    delete_messages_through,
    MessageRow,
)
from voidx.memory.context_frames import (
    build_context_frame,
    load_context_frames,
    save_context_frame,
)
from voidx.memory.jsonl_store import append_session_record

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
