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
