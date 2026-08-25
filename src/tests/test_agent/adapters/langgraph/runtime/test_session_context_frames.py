"""Tests for context frame persistence and loading."""

import json
import sys
from pathlib import Path

from tests.test_agent.conftest import _read_jsonl, _session_dir


import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import voidx.persistence.sqlite as store

from voidx.agent.adapters.persistence.session_repository import (
    create_session,
    get_session,
    delete_session,
    save_message,
    load_messages,
    delete_messages_from,
    delete_messages_through,
    clear_messages,
    MessageRow,
)
from voidx.agent.adapters.persistence.context_frame_repository import (
    build_context_frame,
    gc_context_frames,
    load_context_frames,
    save_context_frame,
)
from voidx.persistence.jsonl import append_session_record

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

        row = await store.fetch_one("SELECT file_path FROM context_frames WHERE id = ?", (frame_id,))
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
            "created_at": store.now(),
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


def _context_jsonl_ids(session_id: str) -> set[int]:
    context_dir = _session_dir(session_id) / "context"
    if not context_dir.exists():
        return set()
    return {
        int(path.stem)
        for path in context_dir.glob("*.jsonl")
        if path.name != "deletes.jsonl" and path.stem.isdigit()
    }


async def _save_kind(session_id: str, frame_kind: str, content: str, user_message_id: int | None = None) -> int:
    return await save_context_frame(build_context_frame(
        session_id=session_id,
        user_message_id=user_message_id,
        frame_kind=frame_kind,
        provider="mimo",
        model="mimo-v2.5",
        messages=[HumanMessage(content=content)],
    ))


async def _insert_legacy_frame(session_id: str, frame_kind: str, content: str) -> int:
    record = build_context_frame(
        session_id=session_id,
        frame_kind=frame_kind,
        provider="mimo",
        model="mimo-v2.5",
        messages=[HumanMessage(content=content)],
    )
    cur = await store.execute_commit(
        """INSERT INTO context_frames (
               session_id, user_message_id, frame_kind, agent_persona, provider,
               model, prefix_hash, frame_hash, message_count, token_estimate,
               file_path, metadata_json, created_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record.session_id,
            record.user_message_id,
            record.frame_kind,
            record.agent_persona,
            record.provider,
            record.model,
            record.prefix_hash,
            record.frame_hash,
            record.message_count,
            record.token_estimate,
            "",
            json.dumps(record.metadata, ensure_ascii=False, sort_keys=True),
            record.created_at,
        ),
    )
    frame_id = cur.lastrowid
    file_path = f"context/{frame_id}.jsonl"
    await store.execute_commit(
        "UPDATE context_frames SET file_path = ? WHERE id = ?",
        (file_path, frame_id),
    )
    context_dir = _session_dir(session_id) / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / f"{frame_id}.jsonl").write_text(
        json.dumps({"role": "user", "content": content}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return frame_id


@pytest.mark.asyncio
async def test_save_context_frame_keeps_five_files_per_kind():
    session = await create_session()
    try:
        main_ids = [await _save_kind(session.id, "main", f"main-{index}") for index in range(6)]
        worker_ids = [await _save_kind(session.id, "worker", f"worker-{index}") for index in range(5)]

        frames = await load_context_frames(session.id)
        disk_ids = _context_jsonl_ids(session.id)

        assert [frame.id for frame in frames if frame.frame_kind == "main"] == main_ids[-5:]
        assert [frame.id for frame in frames if frame.frame_kind == "worker"] == worker_ids
        assert main_ids[0] not in disk_ids
        assert set(main_ids[-5:] + worker_ids) == disk_ids
        assert not (_session_dir(session.id) / "context" / f"{main_ids[0]}.jsonl").exists()
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_delete_messages_through_unlinks_matching_context_files():
    session = await create_session()
    try:
        first_id = await save_message(MessageRow(session_id=session.id, role="user", content="old"))
        second_id = await save_message(MessageRow(session_id=session.id, role="user", content="live"))
        old_frame_id = await _save_kind(session.id, "main", "old", user_message_id=first_id)
        live_frame_id = await _save_kind(session.id, "main", "live", user_message_id=second_id)

        await delete_messages_through(session.id, first_id)

        disk_ids = _context_jsonl_ids(session.id)
        assert old_frame_id not in disk_ids
        assert live_frame_id in disk_ids
        assert [frame.id for frame in await load_context_frames(session.id)] == [live_frame_id]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_gc_context_frames_removes_orphans_and_enforces_retention():
    session = await create_session()
    try:
        live_ids = [await _save_kind(session.id, "main", f"frame-{index}") for index in range(5)]
        stale_ids = [
            await _insert_legacy_frame(session.id, "main", f"stale-{index}")
            for index in range(3)
        ]
        orphan_path = _session_dir(session.id) / "context" / "999001.jsonl"
        orphan_path.write_text('{"role":"user","content":"orphan"}\n', encoding="utf-8")
        deletes_path = _session_dir(session.id) / "context" / "deletes.jsonl"
        deletes_path.write_text(
            json.dumps({
                "type": "context_frame_deleted",
                "mode": "through",
                "last_user_message_id": -1,
                "reason": "keep",
            }) + "\n",
            encoding="utf-8",
        )

        removed = await gc_context_frames(session.id)

        kept_ids = (live_ids + stale_ids)[-5:]
        frames = await load_context_frames(session.id)
        disk_ids = _context_jsonl_ids(session.id)
        assert removed >= 4
        assert [frame.id for frame in frames] == kept_ids
        assert disk_ids == set(kept_ids)
        assert live_ids[0] not in disk_ids
        assert stale_ids[0] in disk_ids
        assert 999001 not in disk_ids
        assert not orphan_path.exists()
        assert deletes_path.exists()
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_delete_messages_through_keeps_null_user_message_id_worker_until_gc():
    session = await create_session()
    try:
        first_id = await save_message(MessageRow(session_id=session.id, role="user", content="old"))
        second_id = await save_message(MessageRow(session_id=session.id, role="user", content="live"))
        old_main_id = await _save_kind(session.id, "main", "old", user_message_id=first_id)
        live_main_id = await _save_kind(session.id, "main", "live", user_message_id=second_id)
        worker_id = await _save_kind(session.id, "worker", "worker-null")

        await delete_messages_through(session.id, first_id)

        disk_ids = _context_jsonl_ids(session.id)
        assert old_main_id not in disk_ids
        assert live_main_id in disk_ids
        assert worker_id in disk_ids
        assert {frame.id for frame in await load_context_frames(session.id)} == {live_main_id, worker_id}
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_clear_messages_unlinks_all_context_jsonl():
    session = await create_session()
    try:
        main_id = await _save_kind(session.id, "main", "main")
        worker_id = await _save_kind(session.id, "worker", "worker")
        compaction_id = await _save_kind(session.id, "compaction", "compaction")

        await clear_messages(session.id)

        disk_ids = _context_jsonl_ids(session.id)
        assert disk_ids == set()
        assert not (_session_dir(session.id) / "context" / f"{main_id}.jsonl").exists()
        assert not (_session_dir(session.id) / "context" / f"{worker_id}.jsonl").exists()
        assert not (_session_dir(session.id) / "context" / f"{compaction_id}.jsonl").exists()
        assert await load_context_frames(session.id) == []
        deletes = _read_jsonl(_session_dir(session.id) / "context" / "deletes.jsonl")
        assert deletes[-1]["mode"] == "all"
        assert deletes[-1]["reason"] == "clear_messages"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_gc_context_frames_deletes_sqlite_row_when_jsonl_missing():
    session = await create_session()
    try:
        live_ids = [await _save_kind(session.id, "main", f"frame-{index}") for index in range(5)]
        stale_id = await _insert_legacy_frame(session.id, "main", "stale-kept")
        missing_id = live_ids[0]
        (_session_dir(session.id) / "context" / f"{missing_id}.jsonl").unlink()

        removed = await gc_context_frames(session.id)

        kept_ids = live_ids[1:] + [stale_id]
        frames = await load_context_frames(session.id)
        disk_ids = _context_jsonl_ids(session.id)
        leftover = await store.fetch_all(
            "SELECT id FROM context_frames WHERE session_id = ? AND id = ?",
            (session.id, missing_id),
        )
        assert removed == 0
        assert leftover == []
        assert [frame.id for frame in frames] == kept_ids
        assert missing_id not in {frame.id for frame in frames}
        assert disk_ids == set(kept_ids)
        assert not (_session_dir(session.id) / "context" / f"{missing_id}.jsonl").exists()
    finally:
        await delete_session(session.id)
