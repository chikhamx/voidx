from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.llm.compaction.service import (
    repair_closed_tool_batches,
    select_closed_tool_tail,
    validate_closed_tool_batches,
)
from voidx.agent.domain.compaction import ClosedToolBatch


def test_validate_closed_tool_batches_empty():
    assert validate_closed_tool_batches([]) is True


def test_validate_closed_tool_batches_pure_assistant():
    msgs = [
        HumanMessage(content="hello"),
        AIMessage(content="world"),
    ]
    assert validate_closed_tool_batches(msgs) is True


def test_validate_closed_tool_batches_matched_tools():
    msgs = [
        AIMessage(
            content="",
            tool_calls=[{"id": "call_1", "name": "read", "args": {}}],
        ),
        ToolMessage(content="ok", tool_call_id="call_1"),
    ]
    assert validate_closed_tool_batches(msgs) is True


def test_validate_closed_tool_batches_multiple_calls_same_ai():
    msgs = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_1", "name": "read", "args": {}},
                {"id": "call_2", "name": "write", "args": {}},
            ],
        ),
        ToolMessage(content="ok1", tool_call_id="call_1"),
        ToolMessage(content="ok2", tool_call_id="call_2"),
    ]
    assert validate_closed_tool_batches(msgs) is True


def test_validate_closed_tool_batches_unclosed_tool_call():
    msgs = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_1", "name": "read", "args": {}},
                {"id": "call_2", "name": "write", "args": {}},
            ],
        ),
        ToolMessage(content="ok1", tool_call_id="call_1"),
    ]
    assert validate_closed_tool_batches(msgs) is False


def test_validate_closed_tool_batches_orphan_tool_message():
    msgs = [
        ToolMessage(content="orphan", tool_call_id="call_nonexistent"),
    ]
    assert validate_closed_tool_batches(msgs) is False


def test_validate_closed_tool_batches_duplicate_tool_result():
    msgs = [
        AIMessage(
            content="",
            tool_calls=[{"id": "call_1", "name": "read", "args": {}}],
        ),
        ToolMessage(content="ok1", tool_call_id="call_1"),
        ToolMessage(content="ok2", tool_call_id="call_1"),
    ]
    assert validate_closed_tool_batches(msgs) is False


def test_select_closed_tool_tail_retains_latest_batch_under_budget():
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "tool1", "args": {}}],
    )
    tool = ToolMessage(content="result", tool_call_id="c1")
    messages = [
        HumanMessage(content="task"),
        ai,
        tool,
    ]
    # token counter that assigns 10 tokens per message
    counter = lambda msgs, model="": len(msgs) * 10
    # context limit 1000 -> 10% tail limit is 100 tokens. 2 messages = 20 tokens <= 100
    tail, source = select_closed_tool_tail(messages, context_limit=1000, token_counter=counter)
    assert tail == [ai, tool]
    assert source == [messages[0]]


def test_select_closed_tool_tail_empty_when_over_budget():
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "tool1", "args": {}}],
    )
    tool = ToolMessage(content="large result" * 100, tool_call_id="c1")
    messages = [
        HumanMessage(content="task"),
        ai,
        tool,
    ]
    # token counter assigns 200 tokens to the tail
    counter = lambda msgs, model="": 200 if len(msgs) == 2 else 10
    # context limit 1000 -> 10% tail limit is 100 tokens. Tail exceeds 100, so tail must be empty
    tail, source = select_closed_tool_tail(messages, context_limit=1000, token_counter=counter)
    assert tail == []
    assert source == messages


def test_select_closed_tool_tail_only_retains_single_latest_batch():
    # Even if two batches both fit within 10%, only the latest complete batch is retained as tail
    b1_ai = AIMessage(content="", tool_calls=[{"id": "c1", "name": "t1", "args": {}}])
    b1_tool = ToolMessage(content="r1", tool_call_id="c1")
    b2_ai = AIMessage(content="", tool_calls=[{"id": "c2", "name": "t2", "args": {}}])
    b2_tool = ToolMessage(content="r2", tool_call_id="c2")
    messages = [
        HumanMessage(content="start"),
        b1_ai,
        b1_tool,
        b2_ai,
        b2_tool,
    ]
    counter = lambda msgs, model="": len(msgs) * 10
    tail, source = select_closed_tool_tail(messages, context_limit=10000, token_counter=counter)
    assert tail == [b2_ai, b2_tool]
    assert source == [messages[0], b1_ai, b1_tool]


def test_repair_closed_tool_batches_empty():
    assert repair_closed_tool_batches([]) == []


def test_repair_closed_tool_batches_already_valid():
    msgs = [
        HumanMessage(content="task"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "read", "args": {}}]),
        ToolMessage(content="res1", tool_call_id="c1"),
        AIMessage(content="answer"),
    ]
    repaired = repair_closed_tool_batches(msgs)
    assert len(repaired) == 4
    assert validate_closed_tool_batches(repaired) is True


def test_repair_closed_tool_batches_unclosed_tail():
    msgs = [
        HumanMessage(content="task"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "c1", "name": "read", "args": {}},
                {"id": "c2", "name": "write", "args": {}},
            ],
        ),
        ToolMessage(content="res1", tool_call_id="c1"),
    ]
    assert validate_closed_tool_batches(msgs) is False
    repaired = repair_closed_tool_batches(msgs)
    assert validate_closed_tool_batches(repaired) is True
    assert len(repaired) == 4
    last_msg = repaired[-1]
    assert isinstance(last_msg, ToolMessage)
    assert last_msg.tool_call_id == "c2"
    assert last_msg.status == "error"


def test_repair_closed_tool_batches_interrupted_by_user():
    msgs = [
        HumanMessage(content="task 1"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "bash", "args": {}}]),
        HumanMessage(content="task 2 (user interrupted before tool finished)"),
        AIMessage(content="task 2 response"),
    ]
    assert validate_closed_tool_batches(msgs) is False
    repaired = repair_closed_tool_batches(msgs)
    assert validate_closed_tool_batches(repaired) is True
    assert len(repaired) == 5
    # The synthetic ToolMessage must be inserted before the second HumanMessage
    assert isinstance(repaired[2], ToolMessage)
    assert repaired[2].tool_call_id == "c1"
    assert repaired[2].status == "error"
    assert isinstance(repaired[3], HumanMessage)
    assert repaired[3].content == "task 2 (user interrupted before tool finished)"


def test_repair_closed_tool_batches_orphan_and_duplicate():
    msgs = [
        ToolMessage(content="orphan", tool_call_id="orphan_id"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "read", "args": {}}]),
        ToolMessage(content="res1", tool_call_id="c1"),
        ToolMessage(content="res1_duplicate", tool_call_id="c1"),
    ]
    assert validate_closed_tool_batches(msgs) is False
    repaired = repair_closed_tool_batches(msgs)
    assert validate_closed_tool_batches(repaired) is True
    assert len(repaired) == 2
    assert isinstance(repaired[0], AIMessage)
    assert isinstance(repaired[1], ToolMessage)
    assert repaired[1].tool_call_id == "c1"


def test_repair_closed_tool_batches_normalizes_ids_with_whitespace():
    msgs = [
        AIMessage(content="", tool_calls=[{"id": "  c1  ", "name": "read", "args": {}}]),
        ToolMessage(content="res1", tool_call_id="c1 "),
    ]
    assert validate_closed_tool_batches(msgs) is False
    repaired = repair_closed_tool_batches(msgs)
    assert validate_closed_tool_batches(repaired) is True
    assert len(repaired) == 2
    assert repaired[0].tool_calls[0]["id"] == "c1"
    assert repaired[1].tool_call_id == "c1"
