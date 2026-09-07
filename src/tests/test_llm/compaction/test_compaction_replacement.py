from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage

from voidx.llm.message_markers import (
    COMPACTION_MESSAGE_MARKER,
    CONTINUATION_MESSAGE_MARKER,
    create_compaction_user_message,
    create_continuation_message,
    is_compaction_message,
    is_continuation_message,
)
from voidx.llm.compaction.summary_input import compaction_summary_messages
from voidx.agent.domain.compaction import CompactionMessageMetadata


def test_compaction_marker_detection():
    normal_user = HumanMessage(content="hello")
    assert is_compaction_message(normal_user) is False

    compaction_user = create_compaction_user_message(
        content="## Goal\n- test",
        metadata=CompactionMessageMetadata(
            compaction_id="c_1",
            source_range_hash="hash_123",
            compaction_depth=1,
            closed_segment_index=1,
            opened_segment_index=2,
            replacement_operation_id="op_1",
        ),
    )
    assert is_compaction_message(compaction_user) is True
    assert compaction_user.additional_kwargs[COMPACTION_MESSAGE_MARKER] is True
    assert compaction_user.additional_kwargs["compaction_id"] == "c_1"
    assert compaction_user.additional_kwargs["replacement_operation_id"] == "op_1"


def test_continuation_marker_detection():
    normal_user = HumanMessage(content="continue")
    assert is_continuation_message(normal_user) is False

    continuation = create_continuation_message()
    assert is_continuation_message(continuation) is True
    assert continuation.additional_kwargs[CONTINUATION_MESSAGE_MARKER] is True


def test_summary_input_retains_previous_compaction_user():
    compaction_user = create_compaction_user_message(
        content="## Previous Summary",
        metadata=CompactionMessageMetadata(
            compaction_id="c_1",
            source_range_hash="h1",
            compaction_depth=1,
            closed_segment_index=1,
            opened_segment_index=2,
            replacement_operation_id="op_1",
        ),
    )
    new_user = HumanMessage(content="new command")
    new_ai = AIMessage(content="new answer")

    source_messages = [
        compaction_user,
        new_user,
        new_ai,
    ]

    summary_head = compaction_summary_messages(source_messages)
    assert len(summary_head) == 3
    assert is_compaction_message(summary_head[0]) is True
    assert summary_head[0].content == "## Previous Summary"
    assert summary_head[1].content == "new command"
    assert summary_head[2].content == "new answer"


def test_summary_input_filters_continuation_and_control_messages():
    compaction_user = create_compaction_user_message(
        content="## Previous Summary",
        metadata=CompactionMessageMetadata(
            compaction_id="c_1",
            source_range_hash="h1",
            compaction_depth=1,
            closed_segment_index=1,
            opened_segment_index=2,
            replacement_operation_id="op_1",
        ),
    )
    continuation = create_continuation_message()
    system_msg = SystemMessage(content="system prompt")
    step_hint = HumanMessage(content="step hint", additional_kwargs={"_voidx_step_hint": True})
    guidance = HumanMessage(content="guidance", additional_kwargs={"_voidx_guidance": True})
    pressure = HumanMessage(content="pressure", additional_kwargs={"_voidx_context_pressure": True})
    raw_ai = AIMessage(content="execution result")

    source_messages = [
        system_msg,
        compaction_user,
        continuation,
        step_hint,
        guidance,
        pressure,
        raw_ai,
    ]

    summary_head = compaction_summary_messages(source_messages)
    assert len(summary_head) == 2
    assert summary_head[0] == compaction_user
    assert summary_head[1] == raw_ai
