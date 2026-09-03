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


@pytest.mark.asyncio
async def test_persist_current_marks_successfully_confirmed_turns_durable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VOIDX_HOME", str(tmp_path / ".voidx"))

    from voidx.presentation.output.dock import BottomInputDock

    presentation_dock = BottomInputDock()
    presentation_dock.begin_capture()
    turn = presentation_dock.start_turn("durable user")
    presentation_dock.end_turn()
    adapter = TranscriptSnapshotAdapter(
        SimpleNamespace(get_dock=lambda: presentation_dock)
    )

    await adapter.persist_current("durable-session")

    assert turn.payload["durable"] is True


@pytest.mark.asyncio
async def test_persist_current_does_not_mark_turn_durable_when_append_fails(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VOIDX_HOME", str(tmp_path / ".voidx"))

    from voidx.presentation.adapters.persistence import transcript_adapter as module
    from voidx.presentation.output.dock import BottomInputDock

    presentation_dock = BottomInputDock()
    presentation_dock.begin_capture()
    turn = presentation_dock.start_turn("retry durable user")
    presentation_dock.end_turn()
    adapter = TranscriptSnapshotAdapter(
        SimpleNamespace(get_dock=lambda: presentation_dock)
    )

    async def fail_append(*_args, **_kwargs):
        raise OSError("durable append failed")

    monkeypatch.setattr(module, "append_transcript_turns", fail_append)

    with pytest.raises(OSError, match="durable append failed"):
        await adapter.persist_current("durable-retry-session")

    assert turn.payload.get("durable") is not True


@pytest.mark.asyncio
async def test_restore_current_marks_complete_transcript_segments_durable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VOIDX_HOME", str(tmp_path / ".voidx"))

    from voidx.presentation.output.dock import BottomInputDock

    source_dock = BottomInputDock()
    source_dock.begin_capture()
    turn = source_dock.start_turn("persisted user")
    source_dock.append_message("persisted answer")
    source_dock.end_turn()
    session_id = "durable-restore-session"
    source_adapter = TranscriptSnapshotAdapter(
        SimpleNamespace(get_dock=lambda: source_dock)
    )

    await source_adapter.persist_current(session_id)

    records = [
        json.loads(line)
        for line in (session_dir(session_id) / "transcript.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    node_records = [record for record in records if record["type"] == "node"]
    assert any(
        record.get("metadata", {}).get("payload", {}).get("durable") is False
        for record in node_records
    )

    restored_dock = BottomInputDock()
    restored_dock.begin_capture()
    restored_adapter = TranscriptSnapshotAdapter(
        SimpleNamespace(get_dock=lambda: restored_dock)
    )

    assert await restored_adapter.restore_current(session_id) is True

    restored_segment = restored_dock.tree.root_turn_segment(
        turn.payload["transcript_turn_id"]
    )
    assert restored_segment
    stack = list(restored_segment)
    while stack:
        node = stack.pop()
        assert node.payload["durable"] is True
        stack.extend(node.children)
