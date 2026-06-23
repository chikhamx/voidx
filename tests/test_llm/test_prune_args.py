"""Tests for prune() — AIMessage tool_calls args dedup logic."""

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.llm.compaction import CompactionService


def _make_messages_with_tool_call(tool_name: str, args: dict, tool_output: str = "ok", has_diff: bool = True) -> list:
    """Build a message list with 3 turns (2 old + 1 recent), where the oldest
    AIMessage has a tool_call with the given name and args."""
    long_tool_output = "x" * 5000  # ensure accumulated > PRUNE_PROTECT
    if has_diff:
        long_tool_output = f"File edited: foo.py\n--- a/foo.py\n+++ b/foo.py\n{long_tool_output}"
    messages = [
        HumanMessage(content="turn 1"),
        AIMessage(
            content="",
            tool_calls=[{"name": tool_name, "args": args, "id": "tc1", "type": "tool_call"}],
            id="ai1",
        ),
        ToolMessage(content=long_tool_output, tool_call_id="tc1", name=tool_name),
        HumanMessage(content="turn 2"),
        AIMessage(content="assistant turn 2", id="ai2"),
        HumanMessage(content="turn 3 (recent)"),
        AIMessage(content="assistant turn 3", id="ai3"),
    ]
    return messages


class TestPruneWriteArgs:
    def test_write_content_omitted_when_large(self):
        """write tool: content should be replaced with [omitted: N lines written] when large."""
        svc = CompactionService()
        content = "line1\nline2\nline3\n"  # 3 lines, 18 chars > placeholder ~28 chars? No, 18 < 28
        # Use something clearly larger
        content = "\n".join(f"line {i}" for i in range(50))  # 50 lines, ~400 chars
        msgs = _make_messages_with_tool_call("write", {"file_path": "foo.py", "content": content})
        original_content = msgs[1].tool_calls[0]["args"]["content"]

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["content"] != original_content
        assert "[omitted:" in msgs[1].tool_calls[0]["args"]["content"]
        assert "50 lines written" in msgs[1].tool_calls[0]["args"]["content"]

    def test_write_content_preserved_when_short(self):
        """write tool: short content should NOT be replaced."""
        svc = CompactionService()
        content = "pass"  # 4 chars < placeholder ~28 chars
        msgs = _make_messages_with_tool_call("write", {"file_path": "foo.py", "content": content})

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["content"] == "pass"

    def test_write_rebuilds_aimessage_not_mutation(self):
        """prune should replace the AIMessage object, not mutate in-place."""
        svc = CompactionService()
        content = "\n".join(f"line {i}" for i in range(50))
        msgs = _make_messages_with_tool_call("write", {"file_path": "foo.py", "content": content})
        original_ai_msg = msgs[1]

        svc.prune(msgs)

        # The object in the list should be a different instance
        assert msgs[1] is not original_ai_msg


class TestPruneReplaceArgs:
    def test_replace_new_string_omitted_when_large(self):
        """replace tool: new_string should be replaced when large."""
        svc = CompactionService()
        new_string = "def hello():\n    print('hi')\n    return True\n" * 5  # ~150 chars
        msgs = _make_messages_with_tool_call("replace", {
            "file_path": "foo.py", "start_no": 1, "end_no": 3,
            "prefix": "def", "suffix": "return", "new_string": new_string,
        })
        original = msgs[1].tool_calls[0]["args"]["new_string"]

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["new_string"] != original
        assert msgs[1].tool_calls[0]["args"]["new_string"] == "[omitted: see diff in tool result]"

    def test_replace_new_string_preserved_when_short(self):
        """replace tool: short new_string should NOT be replaced."""
        svc = CompactionService()
        new_string = "pass"  # 4 chars < placeholder 36 chars
        msgs = _make_messages_with_tool_call("replace", {
            "file_path": "foo.py", "start_no": 1, "end_no": 1,
            "prefix": "old", "suffix": "old", "new_string": new_string,
        })

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["new_string"] == "pass"


class TestPruneLineArgs:
    def test_line_insert_new_string_omitted_when_large(self):
        """line tool (op=insert): new_string should be replaced when large."""
        svc = CompactionService()
        new_string = "def hello():\n    print('hi')\n" * 5
        msgs = _make_messages_with_tool_call("line", {
            "file_path": "foo.py", "op": "insert", "lineno": 10, "new_string": new_string,
        })
        original = msgs[1].tool_calls[0]["args"]["new_string"]

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["new_string"] != original
        assert msgs[1].tool_calls[0]["args"]["new_string"] == "[omitted: see diff in tool result]"

    def test_line_delete_not_touched(self):
        """line tool (op=delete): should NOT be processed even if new_string exists."""
        svc = CompactionService()
        msgs = _make_messages_with_tool_call("line", {
            "file_path": "foo.py", "op": "delete", "lineno": 10,
        })

        svc.prune(msgs)

        # No new_string field in delete, but even if it existed, op=delete should be skipped
        assert msgs[1].tool_calls[0]["args"]["op"] == "delete"

    def test_line_insert_short_content_preserved(self):
        """line tool (op=insert): short new_string should NOT be replaced."""
        svc = CompactionService()
        new_string = "x"  # 1 char < placeholder 36 chars
        msgs = _make_messages_with_tool_call("line", {
            "file_path": "foo.py", "op": "insert", "lineno": 10, "new_string": new_string,
        })

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["new_string"] == "x"


class TestPruneRecentTurnsProtected:
    def test_recent_tool_call_not_pruned(self):
        """Tool calls in the current turn should NOT be pruned."""
        svc = CompactionService()
        content = "\n".join(f"line {i}" for i in range(50))
        # Only 1 turn — should be protected
        messages = [
            HumanMessage(content="turn 1"),
            AIMessage(
                content="",
                tool_calls=[{"name": "write", "args": {"file_path": "foo.py", "content": content}, "id": "tc1", "type": "tool_call"}],
                id="ai1",
            ),
            ToolMessage(content="ok", tool_call_id="tc1", name="write"),
        ]

        svc.prune(messages)

        assert messages[1].tool_calls[0]["args"]["content"] == content

    def test_previous_turn_args_pruned(self):
        """Tool calls in the previous turn (1 turn ago) should be pruned."""
        svc = CompactionService()
        content = "\n".join(f"line {i}" for i in range(50))
        long_tool_output = f"File edited: foo.py\n--- a/foo.py\n+++ b/foo.py\n" + "x" * 5000
        # 2 turns: previous turn has the edit, current turn is recent
        messages = [
            HumanMessage(content="turn 1"),
            AIMessage(
                content="",
                tool_calls=[{"name": "write", "args": {"file_path": "foo.py", "content": content}, "id": "tc1", "type": "tool_call"}],
                id="ai1",
            ),
            ToolMessage(content=long_tool_output, tool_call_id="tc1", name="write"),
            HumanMessage(content="turn 2 (recent)"),
            AIMessage(content="assistant turn 2", id="ai2"),
        ]

        svc.prune(messages)

        assert "[omitted:" in messages[1].tool_calls[0]["args"]["content"]



class TestPruneOtherToolsNotAffected:
    def test_bash_tool_not_touched(self):
        """Non-file-edit tools should not have their args modified."""
        svc = CompactionService()
        long_cmd = "echo " + "a" * 200
        msgs = _make_messages_with_tool_call("bash", {"command": long_cmd})

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["command"] == long_cmd


class TestPruneMultipleToolCalls:
    def test_multiple_tool_calls_in_one_message(self):
        """AIMessage with multiple tool_calls: each should be processed independently."""
        svc = CompactionService()
        long_content = "\n".join(f"line {i}" for i in range(50))
        short_new_string = "x"
        long_tool_output = "--- a/foo.py\n+++ b/foo.py\n" + "x" * 5000
        messages = [
            HumanMessage(content="turn 1"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "write", "args": {"file_path": "a.py", "content": long_content}, "id": "tc1", "type": "tool_call"},
                    {"name": "replace", "args": {"file_path": "b.py", "new_string": short_new_string}, "id": "tc2", "type": "tool_call"},
                ],
                id="ai1",
            ),
            ToolMessage(content=long_tool_output, tool_call_id="tc1", name="write"),
            ToolMessage(content=long_tool_output, tool_call_id="tc2", name="replace"),
            HumanMessage(content="turn 2"),
            AIMessage(content="assistant turn 2", id="ai2"),
            HumanMessage(content="turn 3 (recent)"),
            AIMessage(content="assistant turn 3", id="ai3"),
        ]

        svc.prune(messages)

        # write: long content should be omitted
        assert "[omitted:" in messages[1].tool_calls[0]["args"]["content"]
        # replace: short new_string should be preserved
        assert messages[1].tool_calls[1]["args"]["new_string"] == "x"


class TestPruneFailedEdit:
    def test_args_not_pruned_when_no_diff_in_result(self):
        """When tool result has no diff (edit failed), args should NOT be pruned."""
        svc = CompactionService()
        content = "\n".join(f"line {i}" for i in range(50))
        msgs = _make_messages_with_tool_call("write", {"file_path": "foo.py", "content": content}, has_diff=False)

        svc.prune(msgs)

        # No diff in tool result → args should be preserved
        assert msgs[1].tool_calls[0]["args"]["content"] == content
