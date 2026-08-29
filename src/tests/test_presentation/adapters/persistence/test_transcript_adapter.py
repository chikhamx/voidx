from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from voidx.persistence.jsonl import session_dir
from voidx.presentation.adapters.persistence.transcript_adapter import TranscriptSnapshotAdapter


@pytest.mark.asyncio
async def test_clear_writes_transcript_reset_record(tmp_path, monkeypatch):
    monkeypatch.setenv("VOIDX_HOME", str(tmp_path / ".voidx"))
    adapter = TranscriptSnapshotAdapter(SimpleNamespace(get_dock=lambda: None))

    await adapter.clear("session-1")

    path = session_dir("session-1") / "transcript.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["type"] == "transcript_reset"
    assert records[-1]["reason"] == "clear_messages"


@pytest.mark.asyncio
async def test_persist_current_retries_all_missing_turns_after_append_failure(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VOIDX_HOME", str(tmp_path / ".voidx"))

    from voidx.presentation.adapters.persistence import transcript_adapter as module
    from voidx.presentation.output.tree import OutputTree

    tree = OutputTree()
    dock = SimpleNamespace(tree=tree)
    ui = SimpleNamespace(get_dock=lambda: dock)
    adapter = TranscriptSnapshotAdapter(ui)
    session_id = "session-growth"

    tree.new_node(tree.root, node_type="turn", header="turn 0", status="done")
    await adapter.persist_current(session_id)

    tree.new_node(tree.root, node_type="turn", header="turn 1", status="done")
    real_append = module.append_transcript_turns
    calls = 0

    async def fail_once(session_id, turns):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated append failure")
        return await real_append(session_id, turns)

    monkeypatch.setattr(module, "append_transcript_turns", fail_once)
    with pytest.raises(OSError, match="simulated append failure"):
        await adapter.persist_current(session_id)

    tree.new_node(tree.root, node_type="turn", header="turn 2", status="done")
    await adapter.persist_current(session_id)

    from voidx.presentation.adapters.persistence.transcript_snapshot import load_transcript

    rows = await load_transcript(session_id)
    assert [row.header for row in rows if row.node_type == "turn"] == [
        "turn 0",
        "turn 1",
        "turn 2",
    ]
    records = [
        json.loads(line)
        for line in (session_dir(session_id) / "transcript.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [
        record["turn_id"]
        for record in records
        if record["type"] == "turn_start"
    ] == [0, 1, 2]


@pytest.mark.asyncio
async def test_persist_current_does_not_advance_cursor_when_append_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("VOIDX_HOME", str(tmp_path / ".voidx"))

    from voidx.presentation.adapters.persistence import transcript_adapter as module
    from voidx.presentation.output.tree import OutputTree

    tree = OutputTree()
    dock = SimpleNamespace(tree=tree)
    adapter = TranscriptSnapshotAdapter(SimpleNamespace(get_dock=lambda: dock))
    session_id = "session-retry"
    tree.new_node(tree.root, node_type="turn", header="turn 0", status="done")

    real_append = module.append_transcript_turns
    failed = True

    async def fail_once(session_id, turns):
        nonlocal failed
        if failed:
            failed = False
            raise OSError("simulated append failure")
        return await real_append(session_id, turns)

    monkeypatch.setattr(module, "append_transcript_turns", fail_once)
    with pytest.raises(OSError, match="simulated append failure"):
        await adapter.persist_current(session_id)
    await adapter.persist_current(session_id)

    records = [
        json.loads(line)
        for line in (session_dir(session_id) / "transcript.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [record["type"] for record in records] == [
        "turn_start",
        "node",
        "turn_end",
    ]


@pytest.mark.asyncio
async def test_clear_resets_persist_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("VOIDX_HOME", str(tmp_path / ".voidx"))

    from voidx.presentation.output.tree import OutputTree

    tree = OutputTree()
    dock = SimpleNamespace(tree=tree)
    adapter = TranscriptSnapshotAdapter(SimpleNamespace(get_dock=lambda: dock))
    session_id = "session-clear"
    tree.new_node(tree.root, node_type="turn", header="old", status="done")
    await adapter.persist_current(session_id)
    await adapter.clear(session_id)
    dock.tree = OutputTree()
    dock.tree.new_node(dock.tree.root, node_type="turn", header="new", status="done")
    await adapter.persist_current(session_id)

    records = [
        json.loads(line)
        for line in (session_dir(session_id) / "transcript.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [record["type"] for record in records].count("turn_start") == 2
    assert records[-3]["type"] == "turn_start"
    assert records[-2]["header"] == "new"
