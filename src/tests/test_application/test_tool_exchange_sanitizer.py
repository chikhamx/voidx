from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.infrastructure.langgraph.runtime.streaming import _sanitize_messages_for_replay


def test_streaming_replay_preserves_failed_tool_message():
    error_content = "bash exited with code 1: command not found"
    messages = [
        HumanMessage(content="run the command"),
        AIMessage(
            content="",
            tool_calls=[{"name": "bash", "args": {"command": "bad"}, "id": "call_bash", "type": "tool_call"}],
        ),
        ToolMessage(content=error_content, tool_call_id="call_bash", status="error"),
    ]

    result = _sanitize_messages_for_replay(messages)

    assert result[1] is messages[1]
    assert result[2] is messages[2]
    assert result[1].tool_calls[0]["id"] == "call_bash"
    assert result[2].content == error_content
    assert result[2].status == "error"


def test_streaming_replay_preserves_multiple_failed_tool_rounds():
    messages = [
        HumanMessage(content="start"),
        AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "old", "type": "tool_call"}]),
        ToolMessage(content="old exit 1", tool_call_id="old", status="error"),
        AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "current", "type": "tool_call"}]),
        ToolMessage(content="current exit 2", tool_call_id="current", status="error"),
    ]

    result = _sanitize_messages_for_replay(messages)

    assert [(type(message).__name__, getattr(message, "tool_call_id", None)) for message in result] == [
        ("HumanMessage", None),
        ("AIMessage", None),
        ("ToolMessage", "old"),
        ("AIMessage", None),
        ("ToolMessage", "current"),
    ]
    assert [message.content for message in result if isinstance(message, ToolMessage)] == [
        "old exit 1",
        "current exit 2",
    ]
