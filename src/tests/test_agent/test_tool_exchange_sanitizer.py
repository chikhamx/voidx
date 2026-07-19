import sys
from pathlib import Path


from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.tool_exchange_sanitizer import sanitize_failed_tool_exchanges


def test_sanitize_failed_tool_exchanges_removes_failed_exchange_and_preserves_success_sibling():
    messages = [
        HumanMessage(content="start"),
        AIMessage(
            content=[
                {"type": "tool_use", "id": "call_error", "name": "read", "input": {}},
                {"type": "tool_use", "id": "call_ok", "name": "grep", "input": {}},
            ],
            tool_calls=[
                {"name": "read", "args": {}, "id": "call_error", "type": "tool_call"},
                {"name": "grep", "args": {}, "id": "call_ok", "type": "tool_call"},
            ],
            additional_kwargs={
                "tool_calls": [
                    {"id": "call_error", "function": {"name": "read", "arguments": "{}"}},
                    {"id": "call_ok", "function": {"name": "grep", "arguments": "{}"}},
                ]
            },
        ),
        ToolMessage(content="failed", tool_call_id="call_error", status="error"),
        ToolMessage(content="ok", tool_call_id="call_ok"),
        AIMessage(content="I saw the read failure and will use grep instead."),
    ]

    sanitized = sanitize_failed_tool_exchanges(messages)

    assert [type(message) for message in sanitized] == [HumanMessage, AIMessage, ToolMessage, AIMessage]
    replay_ai = sanitized[1]
    assert isinstance(replay_ai, AIMessage)
    assert [call["id"] for call in replay_ai.tool_calls] == ["call_ok"]
    assert replay_ai.content == [
        {"type": "tool_use", "id": "call_ok", "name": "grep", "input": {}},
    ]
    assert replay_ai.additional_kwargs["tool_calls"] == [
        {"id": "call_ok", "function": {"name": "grep", "arguments": "{}"}},
    ]
    assert isinstance(sanitized[2], ToolMessage)
    assert sanitized[2].tool_call_id == "call_ok"
    assert sanitized[3].content == "I saw the read failure and will use grep instead."


def test_sanitize_failed_tool_exchanges_drops_empty_ai_message():
    messages = [
        HumanMessage(content="start"),
        AIMessage(
            content="",
            tool_calls=[{"name": "read", "args": {}, "id": "call_error", "type": "tool_call"}],
        ),
        ToolMessage(content="failed", tool_call_id="call_error", status="error"),
        HumanMessage(content="next"),
    ]

    sanitized = sanitize_failed_tool_exchanges(messages)

    assert [type(message) for message in sanitized] == [HumanMessage, HumanMessage]
    assert [message.content for message in sanitized] == ["start", "next"]


def test_sanitize_failed_tool_exchanges_preserves_latest_failed_exchange():
    messages = [
        HumanMessage(content="start"),
        AIMessage(
            content="",
            tool_calls=[{"name": "read", "args": {}, "id": "call_error", "type": "tool_call"}],
        ),
        ToolMessage(content="failed", tool_call_id="call_error", status="error"),
    ]

    sanitized = sanitize_failed_tool_exchanges(messages, preserve_latest=True)

    assert len(sanitized) == 3
    assert isinstance(sanitized[1], AIMessage)
    assert isinstance(sanitized[2], ToolMessage)
    assert sanitized[2].status == "error"


def test_sanitize_failed_tool_exchanges_cleans_content_and_additional_kwargs():
    """Verify content blocks and additional_kwargs are cleaned independently."""
    messages = [
        HumanMessage(content="start"),
        AIMessage(
            content=[
                {"type": "tool_use", "id": "call_err", "name": "bash", "input": {"command": "bad"}},
                {"type": "text", "text": "Running bash"},
            ],
            tool_calls=[
                {"name": "bash", "args": {"command": "bad"}, "id": "call_err", "type": "tool_call"},
            ],
            additional_kwargs={
                "tool_calls": [
                    {"id": "call_err", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                ]
            },
        ),
        ToolMessage(content="command not found", tool_call_id="call_err", status="error"),
    ]

    sanitized = sanitize_failed_tool_exchanges(messages)

    assert len(sanitized) == 2
    assert isinstance(sanitized[0], HumanMessage)
    assert isinstance(sanitized[1], AIMessage)
    # content blocks: tool_use removed, text block kept
    assert sanitized[1].content == [{"type": "text", "text": "Running bash"}]
    # tool_calls emptied
    assert sanitized[1].tool_calls == []
    # additional_kwargs.tool_calls removed entirely
    assert "tool_calls" not in sanitized[1].additional_kwargs


def test_streaming_sanitize_applies_failed_exchange_cleanup():
    """Verify _sanitize_messages_for_replay in streaming.py applies failed exchange cleanup."""
    from voidx.agent.infrastructure.langgraph.runtime.streaming import _sanitize_messages_for_replay

    messages = [
        HumanMessage(content="do it"),
        AIMessage(
            content="",
            tool_calls=[{"name": "read", "args": {}, "id": "old_fail", "type": "tool_call"}],
        ),
        ToolMessage(content="no such file", tool_call_id="old_fail", status="error"),
        AIMessage(
            content="",
            tool_calls=[{"name": "grep", "args": {}, "id": "cur_fail", "type": "tool_call"}],
        ),
        ToolMessage(content="no match", tool_call_id="cur_fail", status="error"),
    ]

    sanitized = _sanitize_messages_for_replay(messages)

    # Both rounds preserved (preserve_rounds=2)
    tool_call_ids = []
    for msg in sanitized:
        if isinstance(msg, AIMessage):
            tool_call_ids.extend(call["id"] for call in (msg.tool_calls or []))
    assert "old_fail" in tool_call_ids
    assert "cur_fail" in tool_call_ids


def test_failed_exchange_sanitization_preserves_later_error_summary():
    """Failed tool exchange is removed, but later assistant error summary is preserved."""
    messages = [
        HumanMessage(content="read the file"),
        AIMessage(
            content="",
            tool_calls=[{"name": "read", "args": {}, "id": "call_err", "type": "tool_call"}],
        ),
        ToolMessage(content="permission denied", tool_call_id="call_err", status="error"),
        AIMessage(content="The read failed due to permission denied. I'll try a different approach."),
    ]

    sanitized = sanitize_failed_tool_exchanges(messages)

    assert len(sanitized) == 2
    assert isinstance(sanitized[0], HumanMessage)
    assert isinstance(sanitized[1], AIMessage)
    assert sanitized[1].content == "The read failed due to permission denied. I'll try a different approach."