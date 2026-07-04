import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.todo_state import sanitize_todo_replay_messages


def _ai_with_calls(*calls: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))


def _tool(tool_call_id: str, content: str = "result") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id)


def test_runtime_tool_messages_preserved_across_multiple_todo_and_workflow_rounds():
    messages = [
        HumanMessage(content="start"),
        _ai_with_calls({"id": "todo_1", "name": "todo", "args": {"op": "write"}}),
        _tool("todo_1", "todo write result"),
        _ai_with_calls({"id": "wf_1", "name": "workflow", "args": {"action": "advance"}}),
        _tool("wf_1", "workflow advance result"),
        _ai_with_calls({"id": "todo_2", "name": "todo", "args": {"op": "update"}}),
        _tool("todo_2", "todo update result"),
        _ai_with_calls({"id": "wf_2", "name": "workflow", "args": {"action": "advance"}}),
        _tool("wf_2", "workflow guidance result"),
    ]

    sanitized = sanitize_todo_replay_messages(messages, preserve_latest_tool_exchange=True)

    assert sanitized == messages
    assert [message.tool_call_id for message in sanitized if isinstance(message, ToolMessage)] == [
        "todo_1",
        "wf_1",
        "todo_2",
        "wf_2",
    ]


def test_runtime_tool_content_blocks_are_not_sanitized_for_todo_or_workflow():
    messages = [
        AIMessage(
            content=[
                {"type": "tool_use", "id": "todo_1", "name": "todo", "input": {"op": "write"}},
                {"type": "tool_use", "id": "wf_1", "name": "workflow", "input": {"action": "advance"}},
            ],
            tool_calls=[
                {"id": "todo_1", "name": "todo", "args": {"op": "write"}},
                {"id": "wf_1", "name": "workflow", "args": {"action": "advance"}},
            ],
        ),
        _tool("todo_1"),
        _tool("wf_1"),
    ]

    sanitized = sanitize_todo_replay_messages(messages)

    assert sanitized == messages
