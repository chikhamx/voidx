from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from voidx.agent.adapters.persistence.message_rows import (
    compute_source_range_hash,
    is_compaction_row,
    is_user_turn_row,
    messages_from_rows,
)
from voidx.agent.adapters.persistence.session_repository import (
    MessageRow,
    create_session,
    load_messages,
    replace_effective_message_range,
    save_message,
)
from voidx.agent.domain.compaction import ReplacementResult
from voidx.llm.message_markers import (
    COMPACTION_MESSAGE_MARKER,
    create_compaction_user_message,
)


@pytest.mark.asyncio
async def test_compute_source_range_hash_deterministic(tmp_path):
    rows = [
        MessageRow(id=1, session_id="s1", role="user", content="hello"),
        MessageRow(id=2, session_id="s1", role="assistant", content="world"),
    ]
    h1 = compute_source_range_hash(rows)
    h2 = compute_source_range_hash(rows)
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 64

    rows_modified = [
        MessageRow(id=1, session_id="s1", role="user", content="hello"),
        MessageRow(id=2, session_id="s1", role="assistant", content="world changed"),
    ]
    assert compute_source_range_hash(rows_modified) != h1


def test_is_compaction_row_and_user_turn_row():
    regular_user = MessageRow(id=1, session_id="s1", role="user", content="regular")
    assert is_compaction_row(regular_user) is False
    assert is_user_turn_row(regular_user) is True

    compaction_user = MessageRow(
        id=2,
        session_id="s1",
        role="user",
        content="## Summary",
        additional_kwargs={COMPACTION_MESSAGE_MARKER: True, "compaction_id": "c1"},
    )
    assert is_compaction_row(compaction_user) is True
    # Synthetic summary user must NOT be treated as a real user turn
    assert is_user_turn_row(compaction_user) is False


@pytest.mark.asyncio
async def test_replace_effective_message_range_in_place(tmp_path, monkeypatch):
    import voidx.persistence.jsonl as jsonl_mod
    monkeypatch.setattr(jsonl_mod, "session_dir", lambda sid: tmp_path / sid)

    session = await create_session(workspace=str(tmp_path))
    sid = session.id

    # Add 4 messages: id=1 (user), id=2 (assistant), id=3 (tool), id=4 (assistant)
    m1 = await save_message(MessageRow(session_id=sid, role="user", content="q1"))
    m2 = await save_message(MessageRow(session_id=sid, role="assistant", content="a1"))
    m3 = await save_message(MessageRow(session_id=sid, role="user", content="q2"))
    m4 = await save_message(MessageRow(session_id=sid, role="assistant", content="a2"))

    initial_msgs = await load_messages(sid)
    assert [m.id for m in initial_msgs] == [m1, m2, m3] or [m.id for m in initial_msgs] == [1, 2, 3, 4]
    assert len(initial_msgs) == 4

    # Target source range: [m1, m2] (the first two messages)
    source_rows = initial_msgs[:2]
    source_ids = [r.id for r in source_rows]
    source_hash = compute_source_range_hash(source_rows)

    replacement_row = MessageRow(
        session_id=sid,
        role="user",
        content="## Summary 1",
        additional_kwargs={
            COMPACTION_MESSAGE_MARKER: True,
            "compaction_id": "comp_1",
        },
    )

    result = await replace_effective_message_range(
        session_id=sid,
        source_message_ids=source_ids,
        source_range_hash=source_hash,
        replacement=replacement_row,
        operation_id="op_replace_1",
        closed_segment_index=0,
        opened_segment_index=1,
    )

    assert isinstance(result, ReplacementResult)
    assert result.applied is True
    assert result.operation_id == "op_replace_1"
    assert result.source_message_ids == source_ids

    # Load messages: the first two messages (m1, m2) should be replaced by replacement_row
    # at the exact index 0, followed by m3, m4!
    effective = await load_messages(sid)
    assert len(effective) == 3
    assert effective[0].id == result.replacement_message_id
    assert effective[0].content == "## Summary 1"
    assert is_compaction_row(effective[0]) is True
    assert effective[1].id == m3
    assert effective[1].content == "q2"
    assert effective[2].id == m4
    assert effective[2].content == "a2"


@pytest.mark.asyncio
async def test_replace_effective_message_range_idempotent(tmp_path, monkeypatch):
    import voidx.persistence.jsonl as jsonl_mod
    monkeypatch.setattr(jsonl_mod, "session_dir", lambda sid: tmp_path / sid)

    session = await create_session(workspace=str(tmp_path))
    sid = session.id

    m1 = await save_message(MessageRow(session_id=sid, role="user", content="q1"))
    m2 = await save_message(MessageRow(session_id=sid, role="assistant", content="a1"))

    initial_msgs = await load_messages(sid)
    source_hash = compute_source_range_hash(initial_msgs)
    replacement_row = MessageRow(
        session_id=sid,
        role="user",
        content="## Summary",
        additional_kwargs={COMPACTION_MESSAGE_MARKER: True},
    )

    # First replacement
    res1 = await replace_effective_message_range(
        session_id=sid,
        source_message_ids=[m1, m2],
        source_range_hash=source_hash,
        replacement=replacement_row,
        operation_id="op_idempotent_1",
        closed_segment_index=0,
        opened_segment_index=1,
    )
    assert res1.applied is True

    # Replay same operation_id with same params
    res2 = await replace_effective_message_range(
        session_id=sid,
        source_message_ids=[m1, m2],
        source_range_hash=source_hash,
        replacement=replacement_row,
        operation_id="op_idempotent_1",
        closed_segment_index=0,
        opened_segment_index=1,
    )
    # Should be idempotent and return winner
    assert res2.operation_id == "op_idempotent_1"
    assert res2.replacement_message_id == res1.replacement_message_id

    effective = await load_messages(sid)
    assert len(effective) == 1
    assert effective[0].id == res1.replacement_message_id


@pytest.mark.asyncio
async def test_replace_effective_message_range_hash_mismatch(tmp_path, monkeypatch):
    import voidx.persistence.jsonl as jsonl_mod
    monkeypatch.setattr(jsonl_mod, "session_dir", lambda sid: tmp_path / sid)

    session = await create_session(workspace=str(tmp_path))
    sid = session.id

    m1 = await save_message(MessageRow(session_id=sid, role="user", content="q1"))
    replacement_row = MessageRow(session_id=sid, role="user", content="## Summary")

    with pytest.raises(ValueError, match="hash mismatch|not found"):
        await replace_effective_message_range(
            session_id=sid,
            source_message_ids=[m1],
            source_range_hash="wrong_hash",
            replacement=replacement_row,
            operation_id="op_fail_1",
            closed_segment_index=0,
            opened_segment_index=1,
        )


async def _replacement_case(tmp_path, monkeypatch):
    import voidx.persistence.jsonl as jsonl_mod
    monkeypatch.setattr(jsonl_mod, "session_dir", lambda sid: tmp_path / sid)
    session = await create_session(workspace=str(tmp_path))
    for text in ("one", "two", "three"):
        await save_message(MessageRow(session_id=session.id, role="user", content=text))
    rows = await load_messages(session.id)
    return dict(session_id=session.id, source_message_ids=[r.id for r in rows],
                source_range_hash=compute_source_range_hash(rows),
                replacement=MessageRow(session_id=session.id, role="user", content="summary",
                    additional_kwargs={COMPACTION_MESSAGE_MARKER: True}), operation_id="op")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["role", "marker", "metadata", "reverse", "gap", "segment"])
async def test_replacement_rejects_invalid_submission(tmp_path, monkeypatch, invalid):
    args = await _replacement_case(tmp_path, monkeypatch)
    if invalid == "role":
        args["replacement"].role = "assistant"
    elif invalid == "marker":
        args["replacement"].additional_kwargs = {}
    elif invalid == "metadata":
        args["replacement"].additional_kwargs["replacement_operation_id"] = "other"
    elif invalid == "reverse":
        args["source_message_ids"].reverse()
    elif invalid == "gap":
        rows = await load_messages(args["session_id"])
        args["source_message_ids"] = [rows[0].id, rows[2].id]
        args["source_range_hash"] = compute_source_range_hash([rows[0], rows[2]])
    else:
        args["opened_segment_index"] = 3
    with pytest.raises(ValueError):
        await replace_effective_message_range(**args)
    assert len(await load_messages(args["session_id"])) == 3


@pytest.mark.asyncio
async def test_replacement_operation_conflict_and_cas_winner(tmp_path, monkeypatch):
    args = await _replacement_case(tmp_path, monkeypatch)
    winner = await replace_effective_message_range(**args)
    changed = args["replacement"].model_copy(update={"content": "different"})
    with pytest.raises(ValueError, match="conflict"):
        await replace_effective_message_range(**{**args, "replacement": changed})
    loser = await replace_effective_message_range(**{**args, "operation_id": "loser"})
    assert not loser.applied
    assert loser.winner_operation_id == winner.operation_id
    assert loser.replacement_message_id == winner.replacement_message_id


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["duplicate", "missing", "gap", "role"])
async def test_replacement_replay_rejects_corruption(tmp_path, monkeypatch, corruption):
    from voidx.persistence.jsonl import append_session_record, read_session_records
    from voidx.agent.ports.persistence import GoalRuntimeCorruption
    args = await _replacement_case(tmp_path, monkeypatch)
    await replace_effective_message_range(**args)
    records = await read_session_records(args["session_id"], "messages.jsonl")
    event = records[-1]
    if corruption == "duplicate":
        event["replacement"]["content"] = "tampered"
        await append_session_record(args["session_id"], "messages.jsonl", event)
    else:
        if corruption == "missing":
            del event["replacement"]
        elif corruption == "role":
            event["replacement"]["role"] = "assistant"
        else:
            event["source_message_ids"] = [1, 3]
            event["source_range_hash"] = compute_source_range_hash([
                MessageRow(session_id=args["session_id"], **{k: v for k, v in r.items() if k != "type"})
                for r in (records[0], records[2])])
        import json
        (tmp_path / args["session_id"] / "messages.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records))
    with pytest.raises(GoalRuntimeCorruption):
        await load_messages(args["session_id"])


@pytest.mark.asyncio
async def test_replacement_retry_repairs_crash_count(tmp_path, monkeypatch):
    import voidx.agent.adapters.persistence.session_repository as repo
    args = await _replacement_case(tmp_path, monkeypatch)
    original = repo._refresh_message_count_from_jsonl
    async def crash(sid):
        raise RuntimeError("crash")
    monkeypatch.setattr(repo, "_refresh_message_count_from_jsonl", crash)
    with pytest.raises(RuntimeError, match="crash"):
        await replace_effective_message_range(**args)
    monkeypatch.setattr(repo, "_refresh_message_count_from_jsonl", original)
    await replace_effective_message_range(**args)
    from voidx.persistence.sqlite import fetch_one
    row = await fetch_one("SELECT message_count FROM sessions WHERE id = ?", (args["session_id"],))
    assert row["message_count"] == 1


@pytest.mark.asyncio
async def test_goal_fenced_replacement_effective_reads_and_lease_loss(tmp_path, monkeypatch):
    import voidx.agent.adapters.persistence.session_repository as repo
    import runpy
    from pathlib import Path
    helpers = runpy.run_path(str(Path(__file__).with_name("test_goal_transcript_repository.py")))
    _bind_goal_sessions, _activate_goal_attempt, LEASE_OWNER = (
        helpers[name] for name in ("_bind_goal_sessions", "_activate_goal_attempt", "LEASE_OWNER")
    )
    import voidx.persistence.jsonl as jsonl_mod
    from voidx.persistence.sqlite import execute_commit
    monkeypatch.setattr(jsonl_mod, "session_dir", lambda sid: tmp_path / sid)
    for sid in ("main-session", "work-session", "evaluator-session"):
        await create_session(session_id=sid, profile="goal")
    await _bind_goal_sessions()
    await _activate_goal_attempt("a", session_id="work-session", attempt_number=1, fencing_token=7)
    lease = dict(session_id="work-session", generation="generation-1", attempt_id="a",
                 attempt_number=1, lease_owner=LEASE_OWNER, fencing_token=7)
    for seq in (1, 2):
        await repo.append_goal_transcript_message(**lease, local_sequence=seq,
                                                  message={"role": "user", "content": str(seq)})
    rows = await load_messages("work-session")
    args = dict(source_message_ids=[r.id for r in rows], source_range_hash=compute_source_range_hash(rows),
                replacement=MessageRow(session_id="work-session", role="user", content="summary",
                    additional_kwargs={COMPACTION_MESSAGE_MARKER: True}), operation_id="goal-op")
    with pytest.raises(ValueError, match="Goal"):
        await replace_effective_message_range(session_id="work-session", **args)
    result = await repo.replace_goal_transcript_message_range(**lease, local_sequence=3, **args)
    assert result.applied
    retry = await repo.replace_goal_transcript_message_range(**lease, local_sequence=3, **args)
    assert not retry.applied
    assert [r.content for r in await load_messages("work-session")] == ["summary"]
    assert [r.content for r in await repo.load_goal_transcript_messages("work-session")] == ["summary"]
    assert await repo.count_messages("work-session") == 1
    await repo.append_goal_transcript_message(**lease, local_sequence=4,
                                              message={"role": "assistant", "content": "tail"})
    assert [r.content for r in await load_messages("work-session")] == ["summary", "tail"]
    effective = await load_messages("work-session")
    next_args = {**args, "source_message_ids": [r.id for r in effective],
                 "source_range_hash": compute_source_range_hash(effective), "operation_id": "next-op"}
    original_append = repo.append_session_bytes
    async def append_then_expire(*a, **kw):
        offsets = await original_append(*a, **kw)
        await execute_commit("UPDATE runtime_turn_attempts SET lease_expires_at = 0 WHERE id = 'a'")
        return offsets
    before = (tmp_path / "work-session" / "messages.jsonl").read_bytes()
    monkeypatch.setattr(repo, "append_session_bytes", append_then_expire)
    with pytest.raises(ValueError, match="lease"):
        await repo.replace_goal_transcript_message_range(**lease, local_sequence=5, **next_args)
    assert (tmp_path / "work-session" / "messages.jsonl").read_bytes() == before
    assert [r.content for r in await load_messages("work-session")] == ["summary", "tail"]
    await execute_commit("UPDATE runtime_turn_attempts SET lease_expires_at = 0 WHERE id = 'a'")
    with pytest.raises(ValueError, match="lease"):
        await repo.replace_goal_transcript_message_range(**lease, local_sequence=3, **args)


@pytest.mark.asyncio
async def test_concurrent_replacements_have_one_winner(tmp_path, monkeypatch):
    import asyncio
    args = await _replacement_case(tmp_path, monkeypatch)
    results = await asyncio.gather(*(replace_effective_message_range(**{**args, "operation_id": op})
                                     for op in ("one", "two")))
    assert sum(r.applied for r in results) == 1
    assert len({r.replacement_message_id for r in results}) == 1


@pytest.mark.asyncio
async def test_replacement_load_repairs_crash_count_without_retry(tmp_path, monkeypatch):
    import voidx.agent.adapters.persistence.session_repository as repo
    from voidx.persistence.sqlite import fetch_one
    args = await _replacement_case(tmp_path, monkeypatch)
    async def crash(sid):
        raise RuntimeError("crash")
    monkeypatch.setattr(repo, "_refresh_message_count_from_jsonl", crash)
    with pytest.raises(RuntimeError, match="crash"):
        await replace_effective_message_range(**args)
    assert len(await load_messages(args["session_id"])) == 1
    row = await fetch_one("SELECT message_count FROM sessions WHERE id = ?", (args["session_id"],))
    assert row["message_count"] == 1
