import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.todo_state import _latest_todo_tool_call_ids


def _ai_with_calls(*calls: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))


def _tool(tool_call_id: str) -> ToolMessage:
    return ToolMessage(content="result", tool_call_id=tool_call_id)


def test_no_todo_call_returns_empty_set():
    messages = [
        _ai_with_calls({"id": "bash_1", "name": "bash", "args": {}}),
        _tool("bash_1"),
    ]
    assert _latest_todo_tool_call_ids(messages) == set()


def test_todo_at_trailing_segment_is_preserved():
    messages = [
        _ai_with_calls({"id": "todo_1", "name": "todo", "args": {}}),
        _tool("todo_1"),
    ]
    assert _latest_todo_tool_call_ids(messages) == {"todo_1"}


def test_todo_in_middle_followed_by_other_tools_is_preserved():
    messages = [
        _ai_with_calls({"id": "todo_1", "name": "todo", "args": {}}),
        _tool("todo_1"),
        _ai_with_calls({"id": "bash_1", "name": "bash", "args": {}}),
        _tool("bash_1"),
    ]
    assert _latest_todo_tool_call_ids(messages) == {"todo_1"}


def test_todo_mixed_with_non_todo_in_same_ai_only_keeps_todo():
    messages = [
        _ai_with_calls(
            {"id": "todo_1", "name": "todo", "args": {}},
            {"id": "bash_1", "name": "bash", "args": {}},
        ),
        _tool("todo_1"),
        _tool("bash_1"),
    ]
    assert _latest_todo_tool_call_ids(messages) == {"todo_1"}


def test_multiple_ai_with_todo_keeps_only_latest():
    messages = [
        _ai_with_calls({"id": "todo_A", "name": "todo", "args": {}}),
        _tool("todo_A"),
        _ai_with_calls({"id": "todo_B", "name": "todo", "args": {}}),
        _tool("todo_B"),
    ]
    assert _latest_todo_tool_call_ids(messages) == {"todo_B"}


def test_trailing_workflow_only_does_not_block_todo_preservation():
    messages = [
        _ai_with_calls({"id": "todo_1", "name": "todo", "args": {}}),
        _tool("todo_1"),
        _ai_with_calls({"id": "wf_1", "name": "workflow", "args": {}}),
        _tool("wf_1"),
    ]
    assert _latest_todo_tool_call_ids(messages) == {"todo_1"}
