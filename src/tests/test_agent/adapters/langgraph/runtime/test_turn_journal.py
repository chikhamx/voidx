from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.domain.turn_input import ActiveTurnInput
from voidx.agent.adapters.langgraph.runtime.turn_journal import TurnJournal
from voidx.agent.adapters.langgraph.runtime.topology import latest_user_text
from voidx.llm.message_markers import (
    COMPACTION_MESSAGE_MARKER,
    CONTINUATION_MESSAGE_MARKER,
    create_compaction_user_message,
    create_continuation_message,
)
from voidx.agent.domain.compaction import CompactionMessageMetadata


def test_active_turn_input_creation():
    inp = ActiveTurnInput(
        turn_journal_id="tj_1",
        user_message_id=42,
        raw_text="hello raw",
        semantic_text="hello semantic",
        display_text="hello display",
        title_text="hello title",
        content="hello content",
        content_format="text",
        segment_index=0,
    )
    assert inp.turn_journal_id == "tj_1"
    assert inp.user_message_id == 42
    assert inp.semantic_text == "hello semantic"


def test_latest_user_text_with_active_turn_input():
    inp = ActiveTurnInput(
        turn_journal_id="tj_1",
        user_message_id=42,
        raw_text="user input",
        semantic_text="user semantic",
        display_text="user display",
        title_text="user title",
        content="user content",
        content_format="text",
    )
    # Even if messages list has synthetic compaction user at the end, active_turn_input takes precedence
    compaction_msg = create_compaction_user_message(
        content="## Summary\n- goal",
        metadata=CompactionMessageMetadata(
            compaction_id="c1",
            source_range_hash="h1",
            compaction_depth=1,
            closed_segment_index=0,
            opened_segment_index=1,
            replacement_operation_id="op1",
        ),
    )
    messages = [compaction_msg]
    assert latest_user_text(messages, active_turn_input=inp) == "user semantic"


def test_latest_user_text_skips_compaction_messages():
    real_user = HumanMessage(content="real user command")
    compaction_msg = create_compaction_user_message(
        content="## Summary\n- goal",
        metadata=CompactionMessageMetadata(
            compaction_id="c1",
            source_range_hash="h1",
            compaction_depth=1,
            closed_segment_index=0,
            opened_segment_index=1,
            replacement_operation_id="op1",
        ),
    )
    # Messages has real user, then compaction summary
    messages = [real_user, compaction_msg]
    # latest_user_text must skip compaction_msg and find real_user
    assert latest_user_text(messages) == "real user command"


@pytest.mark.asyncio
async def test_turn_journal_flush_idempotency():
    inp = ActiveTurnInput(
        turn_journal_id="tj_1",
        user_message_id=1,
        raw_text="q",
        semantic_text="q",
        display_text="q",
        title_text="q",
        content="q",
    )
    journal = TurnJournal(active_input=inp)

    saved_rows = []

    async def mock_save(row):
        saved_rows.append(row)
        return len(saved_rows)

    ai_1 = AIMessage(content="answer 1", id="ai_1")
    tool_1 = ToolMessage(content="tool result", tool_call_id="call_1", id="t_1")
    compaction_user = create_compaction_user_message(
        content="## Summary",
        metadata=CompactionMessageMetadata(
            compaction_id="c1",
            source_range_hash="h1",
            compaction_depth=1,
            closed_segment_index=0,
            opened_segment_index=1,
            replacement_operation_id="op1",
        ),
    )
    continuation = create_continuation_message()

    messages = [
        HumanMessage(content="q", id="user_1"),
        ai_1,
        tool_1,
        compaction_user,
        continuation,
    ]

    # First flush (e.g. before rollover)
    flushed_count = await journal.flush(messages, session_id="s1", save_func=mock_save)
    # Only ai_1 and tool_1 should be flushed.
    # User message was saved before turn, synthetic compaction and continuation are never flushed as real messages.
    assert flushed_count == 2
    assert len(saved_rows) == 2
    assert saved_rows[0].role == "assistant"
    assert saved_rows[0].content == "answer 1"
    assert saved_rows[1].role == "tool"
    assert saved_rows[1].content == "tool result"

    # Second flush with same messages must be completely idempotent!
    flushed_count_2 = await journal.flush(messages, session_id="s1", save_func=mock_save)
    assert flushed_count_2 == 0
    assert len(saved_rows) == 2

    # Now simulate rollover where live messages list is reset:
    # live_messages = [compaction_user, continuation, new_ai]
    new_ai = AIMessage(content="final answer after rollover", id="ai_2")
    live_after_rollover = [
        compaction_user,
        continuation,
        new_ai,
    ]

    # Final flush at turn completion
    flushed_count_3 = await journal.flush(live_after_rollover, session_id="s1", save_func=mock_save)
    assert flushed_count_3 == 1
    assert len(saved_rows) == 3
    assert saved_rows[2].content == "final answer after rollover"
