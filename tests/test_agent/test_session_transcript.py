"""Tests for transcript node persistence and replay."""

import json
import sys
from pathlib import Path

from tests.test_agent.conftest import _read_jsonl, _session_dir, _table_names

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

import voidx.memory.store as store
import voidx.memory.jsonl_store as jsonl_store

from voidx.memory.session import (
    create_session,
    get_session,
    delete_session,
    save_message,
    load_messages,
    MessageRow,
)
from voidx.memory.transcript import (
    TranscriptNodeRow,
    append_transcript_summary,
    load_transcript,
    replace_transcript,
)
from voidx.memory.jsonl_store import append_session_record

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
