from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.adapters.langgraph.runtime.topology import latest_user_text
from voidx.agent.adapters.langgraph.runtime.convergence import generate_fallback_summary
from voidx.agent.domain.turn_input import ActiveTurnInput
from voidx.agent.adapters.langgraph.runtime.turn_journal import TurnJournal
from voidx.agent.domain.compaction import CompactionMessageMetadata
from voidx.llm.message_markers import create_compaction_user_message
from voidx.llm.compaction.service import validate_closed_tool_batches
from voidx.agent.adapters.persistence.session_repository import (
    MessageRow,
    create_session,
    load_messages,
    replace_effective_message_range,
    save_message,
    materialize_legacy_summary_if_needed,
)
from voidx.agent.adapters.persistence.message_rows import compute_source_range_hash
from voidx.agent.ports.persistence import GoalRuntimeCorruption


def test_latest_user_text_with_dict_active_turn_input():
    # Finding 2: active_turn_input as dict from AgentState
    state_input = {
        "turn_journal_id": "tj_1",
        "raw_text": "raw user text",
        "semantic_text": "semantic user text",
        "title_text": "title",
    }
    messages = [create_compaction_user_message("## Summary", CompactionMessageMetadata(
        compaction_id="c1",
        source_range_hash="h1",
        replacement_operation_id="op1",
    ))]
    assert latest_user_text(messages, active_turn_input=state_input) == "semantic user text"


def test_convergence_generate_fallback_summary_skips_compaction_messages():
    # Finding 4: convergence skips compaction messages and respects active_turn_input
    compaction_msg = create_compaction_user_message("## Summary", CompactionMessageMetadata(
        compaction_id="c1",
        source_range_hash="h1",
        replacement_operation_id="op1",
    ))
    state = {
        "step_count": 5,
        "max_steps": 5,
        "messages": [compaction_msg],
        "active_turn_input": {
            "semantic_text": "user intention from active input",
        },
    }
    summary = generate_fallback_summary(state)
    assert "user intention from active input" in summary
    assert "## Summary" not in summary


def test_validate_closed_tool_batches_malformed_tool_calls():
    # Finding 7: malformed tool_calls must be rejected, not silently skipped
    m1 = AIMessage(content="")
    m1.tool_calls = ["not a dict"]  # type: ignore
    assert validate_closed_tool_batches([m1]) is False

    m2 = AIMessage(content="")
    m2.tool_calls = [{"name": "read"}]  # missing id
    assert validate_closed_tool_batches([m2]) is False


@pytest.mark.asyncio
async def test_load_messages_jsonl_detects_corrupted_replacement_event(tmp_path, monkeypatch):
    # Finding 5: corrupted message_replaced in jsonl raises error
    import voidx.persistence.jsonl as jsonl_mod
    monkeypatch.setattr(jsonl_mod, "session_dir", lambda sid: tmp_path / sid)

    session = await create_session(workspace=str(tmp_path))
    sid = session.id

    # Append a bad message_replaced record directly to messages.jsonl where source_message_ids do not exist
    bad_record = {
        "type": "message_replaced",
        "operation_id": "op_corrupted",
        "source_message_ids": [9999, 10000],
        "source_range_hash": "bad_hash",
        "replacement": {
            "id": 100,
            "role": "user",
            "content": "summary",
        },
    }
    await jsonl_mod.append_session_record(sid, "messages.jsonl", bad_record)

    with pytest.raises(GoalRuntimeCorruption):
        await load_messages(sid)


@pytest.mark.asyncio
async def test_materialize_legacy_summary_if_needed(tmp_path, monkeypatch):
    # Finding 6: legacy summary migration
    import voidx.persistence.jsonl as jsonl_mod
    monkeypatch.setattr(jsonl_mod, "session_dir", lambda sid: tmp_path / sid)

    session = await create_session(workspace=str(tmp_path))
    sid = session.id

    await save_message(MessageRow(session_id=sid, role="user", content="msg 1"))

    # Session has a legacy compaction_summary string, but no synthetic compaction row yet
    created_id = await materialize_legacy_summary_if_needed(
        sid,
        legacy_summary="## Legacy Long Summary",
    )
    assert created_id is not None

    msgs = await load_messages(sid)
    assert len(msgs) == 2
    assert msgs[0].id == created_id
    assert msgs[0].role == "user"
    assert msgs[0].content == "## Legacy Long Summary"
    assert msgs[0].additional_kwargs.get("_voidx_compaction_message") is True
    assert msgs[0].additional_kwargs.get("compaction_depth") == 0

    # Idempotent: repeating does not create a second one
    created_id_again = await materialize_legacy_summary_if_needed(
        sid,
        legacy_summary="## Legacy Long Summary",
    )
    assert created_id_again is None
    msgs_after = await load_messages(sid)
    assert len(msgs_after) == 2
