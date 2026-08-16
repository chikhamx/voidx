"""Tests for transcript node persistence and replay."""

import json
import sys
from pathlib import Path

from tests.test_agent.conftest import _read_jsonl, _session_dir, _table_names


import pytest

import voidx.persistence.sqlite as store
import voidx.persistence.jsonl as jsonl_store

from voidx.agent.adapters.persistence.session_repository import (
    create_session,
    get_session,
    delete_session,
    save_message,
    load_messages,
    MessageRow,
)
from voidx.presentation.adapters.persistence.transcript_snapshot import (
    TranscriptNodeRow,
    load_transcript,
    load_transcript_page,
    replace_transcript,
)
from voidx.persistence.jsonl import append_session_record, append_session_records


async def _append_summary_record(session_id: str) -> None:
    offset = await append_session_record(session_id, "transcript.jsonl", {
        "type": "summary",
        "turn_id": 0,
        "content": "older context summary",
        "created_at": "test",
    })
    index_path = _session_dir(session_id) / "transcript.idx.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["transcript_size"] = (_session_dir(session_id) / "transcript.jsonl").stat().st_size
    index["summary_offsets"] = {"0": offset}
    index_path.write_text(json.dumps(index), encoding="utf-8")

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
        assert index["version"] == 2
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
        index_path = _session_dir(session.id) / "transcript.idx.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        offsets, transcript_size = await append_session_records(
            session.id,
            "transcript.jsonl",
            [
                {"type": "transcript_reset", "reason": "legacy_replace", "created_at": "test"},
                {"type": "turn_start", "turn_id": 0, "timestamp": "test"},
                {
                    "type": "node",
                    "turn_id": 0,
                    "node_id": 0,
                    "parent_node_id": None,
                    "sort_order": 0,
                    "node_type": "turn",
                    "header": "latest",
                    "body_lines": [],
                    "status": "running",
                    "collapsed": False,
                    "created_at": "test",
                    "updated_at": "test",
                    "metadata": {},
                },
                {"type": "turn_end", "turn_id": 0, "timestamp": "test"},
            ],
        )
        index.update(
            transcript_size=transcript_size,
            last_reset_offset=offsets[0],
            turn_offsets={"0": offsets[1]},
        )
        index_path.write_text(json.dumps(index), encoding="utf-8")

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
        await _append_summary_record(session.id)
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
        await _append_summary_record(session.id)
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


@pytest.mark.asyncio
async def test_replace_transcript_does_not_accumulate_previous_snapshot_records():
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
                    header="old snapshot",
                )
            ],
            turn_count=1,
        )
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header="new snapshot",
                )
            ],
            turn_count=1,
        )

        records = _read_jsonl(_session_dir(session.id) / "transcript.jsonl")
        assert [record["type"] for record in records] == [
            "transcript_reset",
            "turn_start",
            "node",
            "turn_end",
        ]
        assert records[2]["header"] == "new snapshot"
        assert "old snapshot" not in (_session_dir(session.id) / "transcript.jsonl").read_text(
            encoding="utf-8"
        )
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_page_reads_latest_window_and_before_cursor():
    session = await create_session()
    try:
        nodes = []
        for turn_id in range(4):
            nodes.extend([
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=turn_id,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header=f"turn {turn_id}",
                ),
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=turn_id,
                    node_id=1,
                    parent_node_id=0,
                    sort_order=1,
                    node_type="assistant",
                    header=f"reply {turn_id}",
                    body_lines=[f"body {turn_id}"],
                    status="done",
                ),
            ])
        await replace_transcript(session.id, nodes, turn_count=4)

        latest = await load_transcript_page(session.id, turn_limit=2)

        assert [row.turn_id for row in latest.rows] == [2, 2, 3, 3]
        assert latest.before_turn_id == 2
        assert latest.after_turn_id == 3
        assert latest.has_earlier is True
        assert latest.has_later is False

        earlier = await load_transcript_page(
            session.id,
            before_turn_id=latest.before_turn_id,
            turn_limit=2,
        )

        assert [row.turn_id for row in earlier.rows] == [0, 0, 1, 1]
        assert earlier.before_turn_id == 0
        assert earlier.after_turn_id == 1
        assert earlier.has_earlier is False
        assert earlier.has_later is True
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_page_falls_back_when_index_is_missing():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=turn_id,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header=f"turn {turn_id}",
                )
                for turn_id in range(3)
            ],
            turn_count=3,
        )
        (_session_dir(session.id) / "transcript.idx.json").unlink()

        page = await load_transcript_page(session.id, turn_limit=2)

        assert [row.turn_id for row in page.rows] == [1, 2]
        assert page.has_earlier is True
        assert page.has_later is False
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_page_seeks_turn_offset_without_loading_checkpoint(monkeypatch):
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=turn_id,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header=f"turn {turn_id}",
                )
                for turn_id in range(3)
            ],
            turn_count=3,
        )
        from voidx.presentation.adapters.persistence import transcript_snapshot as module

        monkeypatch.setattr(
            module,
            "_load_transcript_checkpoint",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("paged reads must seek turn offsets")
            ),
        )

        page = await load_transcript_page(session.id, turn_limit=1)

        assert [row.turn_id for row in page.rows] == [2]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_page_falls_back_for_legacy_records_without_turn_offsets():
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
        _, transcript_size = await append_session_records(
            session.id,
            "transcript.jsonl",
            [
                {"type": "transcript_reset", "reason": "legacy", "created_at": "test"},
                {"type": "turn_start", "turn_id": 1, "timestamp": "test"},
                {
                    "type": "node",
                    "turn_id": 1,
                    "node_id": 0,
                    "parent_node_id": None,
                    "sort_order": 0,
                    "node_type": "turn",
                    "header": "legacy latest",
                    "body_lines": [],
                    "status": "done",
                    "collapsed": False,
                    "created_at": "test",
                    "updated_at": "test",
                    "metadata": {},
                },
                {"type": "turn_end", "turn_id": 1, "timestamp": "test"},
            ],
        )
        index_path = _session_dir(session.id) / "transcript.idx.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["transcript_size"] = transcript_size
        index_path.write_text(json.dumps(index), encoding="utf-8")

        page = await load_transcript_page(session.id, turn_limit=1)

        assert [row.header for row in page.rows] == ["legacy latest"]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_page_includes_turn_appended_after_stale_index():
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
                header="old turn",
            )],
            turn_count=1,
        )
        summary_offset = await append_session_record(session.id, "transcript.jsonl", {
            "type": "summary",
            "turn_id": 0,
            "content": "summary",
            "created_at": "test",
        })
        _, transcript_size = await append_session_records(
            session.id,
            "transcript.jsonl",
            [{
                "type": "node",
                "turn_id": 1,
                "node_id": 0,
                "parent_node_id": None,
                "sort_order": 0,
                "node_type": "turn",
                "header": "new turn",
                "body_lines": [],
                "status": "done",
                "collapsed": False,
                "created_at": "test",
                "updated_at": "test",
                "metadata": {},
            }],
        )
        index_path = _session_dir(session.id) / "transcript.idx.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["transcript_size"] = transcript_size
        index["summary_offsets"] = {"0": summary_offset}
        index_path.write_text(json.dumps(index), encoding="utf-8")

        page = await load_transcript_page(session.id, turn_limit=1)

        assert [row.header for row in page.rows] == ["new turn"]
        assert page.before_turn_id == 1
        assert page.after_turn_id == 1
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_replace_transcript_writes_bounded_turn_ranges():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=turn_id,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header=f"turn {turn_id}",
                )
                for turn_id in range(3)
            ],
            turn_count=3,
        )

        index = json.loads(
            (_session_dir(session.id) / "transcript.idx.json").read_text(encoding="utf-8")
        )

        assert index["version"] == 2
        assert index["range_readable"] is True
        assert index["indexed_end_offset"] == index["transcript_size"]
        assert set(index["turn_ranges"]) == {"0", "1", "2"}
        assert all(
            start < end
            for start, end in index["turn_ranges"].values()
        )
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_page_reads_only_selected_turn_range(monkeypatch):
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=turn_id,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header=f"turn {turn_id}",
                )
                for turn_id in range(5)
            ],
            turn_count=5,
        )
        from voidx.presentation.adapters.persistence import transcript_snapshot as module

        calls: list[tuple[str, str, int, int]] = []

        async def read_between(
            session_id: str,
            filename: str,
            start: int,
            end: int,
        ) -> list[dict]:
            calls.append((session_id, filename, start, end))
            records: list[dict] = []
            path = _session_dir(session_id) / filename
            with path.open("rb") as stream:
                stream.seek(start)
                while stream.tell() < end:
                    raw_line = stream.readline()
                    if not raw_line:
                        break
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    record = json.loads(raw_line.decode("utf-8"))
                    if isinstance(record, dict):
                        records.append(record)
            return records

        monkeypatch.setattr(
            module,
            "read_session_records_between_offsets",
            read_between,
            raising=False,
        )

        page = await load_transcript_page(
            session.id,
            before_turn_id=4,
            turn_limit=1,
        )

        assert [row.header for row in page.rows] == ["turn 3"]
        assert len(calls) == 1
        _, filename, start, end = calls[0]
        assert filename == "transcript.jsonl"
        assert start < end < (_session_dir(session.id) / filename).stat().st_size
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_noncanonical_append_disables_bounded_transcript_page(monkeypatch):
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [TranscriptNodeRow(
                session_id=session.id,
                turn_id=0,
                node_id=0,
                sort_order=0,
                node_type="thought",
                header="Thinking",
                body_lines=["before"],
                status="running",
            )],
            turn_count=1,
        )
        await append_session_record(session.id, "transcript.jsonl", {
            "type": "node_update",
            "turn_id": 0,
            "node_id": 0,
            "status": "done",
            "body_append": ["after"],
        })
        from voidx.presentation.adapters.persistence import transcript_snapshot as module

        async def unexpected_range_read(*_args, **_kwargs):
            raise AssertionError("noncanonical transcript must use fallback reading")

        monkeypatch.setattr(
            module,
            "read_session_records_between_offsets",
            unexpected_range_read,
            raising=False,
        )

        page = await load_transcript_page(session.id, turn_limit=1)
        index = json.loads(
            (_session_dir(session.id) / "transcript.idx.json").read_text(encoding="utf-8")
        )

        assert [row.body_lines for row in page.rows] == [["before", "after"]]
        assert index["version"] == 2
        assert index["range_readable"] is False
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_load_transcript_page_falls_back_when_range_contains_same_size_corruption():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=turn_id,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header=f"turn {turn_id}",
                )
                for turn_id in range(2)
            ],
            turn_count=2,
        )
        path = _session_dir(session.id) / "transcript.jsonl"
        data = path.read_bytes()
        corrupt_at = data.index(b'"header":"turn 1"')
        path.write_bytes(data[:corrupt_at] + b"\xff" + data[corrupt_at + 1:])

        page = await load_transcript_page(session.id, turn_limit=1)

        assert [row.header for row in page.rows] == ["turn 1"]
    finally:
        await delete_session(session.id)
