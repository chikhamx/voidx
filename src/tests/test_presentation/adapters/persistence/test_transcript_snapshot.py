from __future__ import annotations

import json

import pytest

import voidx.persistence.sqlite as store
from voidx.persistence.jsonl import append_session_records, session_dir
from voidx.presentation.adapters.persistence.transcript_snapshot import (
    TranscriptNodeRow,
    append_transcript_turns,
    compact_transcript,
    load_transcript,
    replace_transcript,
)


@pytest.mark.asyncio
async def test_load_discards_incomplete_tail_transaction_and_rebuilds_index(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    session_id = "session-1"

    await replace_transcript(
        session_id,
        [
            TranscriptNodeRow(
                session_id=session_id,
                turn_id=0,
                node_id=0,
                sort_order=0,
                node_type="turn",
                header="complete",
                status="done",
            )
        ],
        turn_count=1,
    )

    transcript_path = session_dir(session_id) / "transcript.jsonl"
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "turn_start", "turn_id": 1}) + "\n")
        handle.write(
            json.dumps(
                {
                    "type": "node",
                    "turn_id": 1,
                    "node_id": 0,
                    "sort_order": 0,
                    "node_type": "turn",
                    "header": "incomplete",
                }
            )
            + "\n"
        )
        handle.write('{"type":"node","turn_id":1,')

    rows = await load_transcript(session_id)

    assert [(row.turn_id, row.node_id, row.header) for row in rows] == [
        (0, 0, "complete")
    ]
    index = json.loads(
        (session_dir(session_id) / "transcript.idx.json").read_text(encoding="utf-8")
    )
    assert set(index["turn_ranges"]) == {"0"}
    assert set(index["turn_offsets"]) == {"0"}


@pytest.mark.asyncio
async def test_append_transcript_turns_is_idempotent_for_duplicate_turn_id(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    session_id = "duplicate-turn"
    row = TranscriptNodeRow(
        session_id=session_id,
        turn_id=0,
        node_id=0,
        sort_order=0,
        node_type="turn",
        header="first",
        status="done",
    )

    assert await append_transcript_turns(session_id, [(0, [row])]) == [0]
    retry = row.model_copy(update={"header": "retry"})
    assert await append_transcript_turns(session_id, [(0, [retry])]) == []

    records = [
        json.loads(line)
        for line in (session_dir(session_id) / "transcript.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [record["type"] for record in records].count("turn_start") == 1
    rows = await load_transcript(session_id)
    assert [(row.turn_id, row.header) for row in rows] == [(0, "first")]


@pytest.mark.asyncio
async def test_append_transcript_turns_recovers_duplicate_after_incomplete_tail(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    session_id = "retry-after-tail"
    await replace_transcript(
        session_id,
        [
            TranscriptNodeRow(
                session_id=session_id,
                turn_id=0,
                node_id=0,
                sort_order=0,
                node_type="turn",
                header="complete",
                status="done",
            )
        ],
        turn_count=1,
    )
    path = session_dir(session_id) / "transcript.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "turn_start", "turn_id": 1}) + "\n")
        handle.write(json.dumps({"type": "node", "turn_id": 1, "node_id": 0, "sort_order": 0, "node_type": "turn", "header": "partial"}) + "\n")

    row = TranscriptNodeRow(
        session_id=session_id,
        turn_id=1,
        node_id=0,
        sort_order=0,
        node_type="turn",
        header="retried",
        status="done",
    )
    assert await append_transcript_turns(session_id, [(1, [row])]) == [1]
    assert await append_transcript_turns(session_id, [(1, [row])]) == []
    rows = await load_transcript(session_id)
    assert [(row.turn_id, row.header) for row in rows] == [
        (0, "complete"),
        (1, "retried"),
    ]


def test_tree_to_transcript_turn_rows_includes_root_siblings_in_one_turn():
    from voidx.presentation.output.dock import BottomInputDock
    from voidx.presentation.adapters.persistence.transcript_snapshot import (
        tree_to_transcript_turn_rows,
    )

    dock = BottomInputDock()
    dock.begin_capture()
    dock.start_turn("question")
    dock.append_thought("checked context", elapsed=1.0)
    tool = dock.start_tool(
        "Reading",
        'file_path="src/app.py"',
        tool_call_id="call_read",
        tool_name="read",
    )
    dock.append_tool_result(
        "src/app.py\nprint('ok')",
        parent=tool,
        tool_call_id="call_read",
        collapsed=False,
    )
    dock.set_todo_state(
        "1/1 done · 0 active · 0 pending",
        [{"id": "review", "content": "finish review", "status": "done"}],
    )
    dock.commit_todo_state()

    rows = tree_to_transcript_turn_rows("session-1", dock.tree, 0)

    assert {row.node_type for row in rows} >= {
        "turn",
        "assistant",
        "thought",
        "tool_call",
        "tool_result",
        "todo",
    }
    assert any(row.tool_call_id == "call_read" for row in rows)



def test_incomplete_transaction_discards_cross_turn_node_records():
    from voidx.presentation.adapters.persistence.transcript_snapshot import (
        _complete_transaction_records,
    )

    records = [
        {"type": "turn_start", "turn_id": 1},
        {
            "type": "node",
            "turn_id": 1,
            "node_id": 0,
            "sort_order": 0,
            "node_type": "turn",
        },
        {
            "type": "node",
            "turn_id": 2,
            "node_id": 0,
            "sort_order": 0,
            "node_type": "turn",
        },
        {
            "type": "node_update",
            "turn_id": 2,
            "node_id": 0,
            "header": "leaked",
        },
    ]

    assert _complete_transaction_records(records) == []


@pytest.mark.asyncio
async def test_compact_transcript_collapses_old_snapshots_without_changing_rows(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    session_id = "legacy-compaction"

    await replace_transcript(
        session_id,
        [
            TranscriptNodeRow(
                session_id=session_id,
                turn_id=0,
                node_id=0,
                sort_order=0,
                node_type="turn",
                header="initial",
                status="done",
            )
        ],
        turn_count=1,
    )

    for label, turn_count in (("one", 4), ("two", 8), ("three", 12)):
        records = [
            {
                "type": "transcript_reset",
                "reason": f"legacy-{label}",
                "created_at": label,
            }
        ]
        for turn_id in range(turn_count):
            records.extend(
                [
                    {"type": "turn_start", "turn_id": turn_id, "timestamp": label},
                    {
                        "type": "node",
                        "turn_id": turn_id,
                        "node_id": 0,
                        "parent_node_id": None,
                        "sort_order": 0,
                        "node_type": "turn",
                        "header": f"{label} turn {turn_id}",
                        "body_lines": [],
                        "status": "done",
                        "collapsed": False,
                        "metadata": {},
                    },
                    {"type": "turn_end", "turn_id": turn_id, "timestamp": label},
                ]
            )
        await append_session_records(session_id, "transcript.jsonl", records)

    path = session_dir(session_id) / "transcript.jsonl"
    rows_before = await load_transcript(session_id)
    records_before = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    size_before = path.stat().st_size

    await compact_transcript(session_id)

    rows_after = await load_transcript(session_id)
    records_after = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]

    assert [
        row.model_dump(mode="json", exclude={"created_at", "updated_at"})
        for row in rows_after
    ] == [
        row.model_dump(mode="json", exclude={"created_at", "updated_at"})
        for row in rows_before
    ]
    assert len([record for record in records_before if record["type"] == "transcript_reset"]) == 4
    assert len([record for record in records_after if record["type"] == "transcript_reset"]) == 1
    assert path.stat().st_size < size_before
