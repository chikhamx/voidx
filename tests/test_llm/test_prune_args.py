"""Tests for prune() — AIMessage tool_calls args dedup logic."""

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.llm import compaction
from voidx.llm.compaction import CompactionService, PRUNE_PROTECTED_TOOLS, TOOL_OUTPUT_MAX_CHARS


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
                                                            "file_path": "foo.py",
                                                            "bounds": [{"line_no": 1, "anchor": "def"}, {"line_no": 3, "anchor": "return"}],
                                                            "new_string": new_string,
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
                                                            "file_path": "foo.py",
                                                            "bounds": [{"line_no": 1, "anchor": "old"}],
                                                            "new_string": new_string,
                                                        })

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["new_string"] == "pass"


class TestPruneLineArgs:
    def test_line_insert_new_string_omitted_when_large(self):
        """line tool (op=insert): new_string should be replaced when large."""
        svc = CompactionService()
        new_string = "def hello():\n    print('hi')\n" * 5
        msgs = _make_messages_with_tool_call("write", {
            "file_path": "foo.py", "op": "insert", "lineno": 10, "new_string": new_string,
        })
        original = msgs[1].tool_calls[0]["args"]["new_string"]

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["new_string"] != original
        assert msgs[1].tool_calls[0]["args"]["new_string"] == "[omitted: see diff in tool result]"

    def test_line_insert_short_content_preserved(self):
        """line tool (op=insert): short new_string should NOT be replaced."""
        svc = CompactionService()
        new_string = "x"  # 1 char < placeholder 36 chars
        msgs = _make_messages_with_tool_call("write", {
            "file_path": "foo.py", "op": "insert", "lineno": 10, "new_string": new_string,
        })

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["new_string"] == "x"


    def test_line_append_new_string_omitted_when_large(self):
        """line tool (op=append): new_string should be replaced when large."""
        svc = CompactionService()
        new_string = "def hello():\n    print('hi')\n" * 5
        msgs = _make_messages_with_tool_call("write", {
            "file_path": "foo.py", "op": "append", "new_string": new_string,
        })
        original = msgs[1].tool_calls[0]["args"]["new_string"]

        svc.prune(msgs)

        assert msgs[1].tool_calls[0]["args"]["new_string"] != original
        assert msgs[1].tool_calls[0]["args"]["new_string"] == "[omitted: see diff in tool result]"

    def test_line_append_short_content_preserved(self):
        """line tool (op=append): short new_string should NOT be replaced."""
        svc = CompactionService()
        new_string = "x"
        msgs = _make_messages_with_tool_call("write", {
            "file_path": "foo.py", "op": "append", "new_string": new_string,
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


# ── ToolMessage truncation turn protection ──────────────────────────────

LARGE_OUTPUT = "x" * 3000  # > TOOL_OUTPUT_MAX_CHARS (2000)
TRUNCATED_PREFIX = LARGE_OUTPUT[:TOOL_OUTPUT_MAX_CHARS]


def _make_multiturn_messages(tool_outputs: list[tuple[str, str]]) -> list:
    """Build a message list with N turns, each having one bash tool call.

    Each tuple: (tool_call_id, tool_output_content)
    The last turn has a final AIMessage without tool_calls (assistant reply).
    """
    messages = []
    for i, (tc_id, output) in enumerate(tool_outputs):
        messages.append(HumanMessage(content=f"turn {i + 1}"))
        messages.append(AIMessage(
            content="",
            tool_calls=[{"name": "bash", "args": {"command": f"cmd{i}"}, "id": tc_id, "type": "tool_call"}],
            id=f"ai_tc_{i}",
        ))
        messages.append(ToolMessage(content=output, tool_call_id=tc_id, name="bash"))
    # Final assistant reply
    messages.append(HumanMessage(content=f"turn {len(tool_outputs) + 1} (current)"))
    messages.append(AIMessage(content="assistant reply", id="ai_final"))
    return messages


class TestPruneToolMessageTurnProtection:
    """ToolMessage truncation must respect turn boundaries:
    most recent 2 turns are protected, older turns can be truncated.
    """

    def test_recent_turns_tool_output_protected(self, monkeypatch):
        """ToolMessages in turns_seen 0/1 keep full content."""
        monkeypatch.setattr(compaction, "PRUNE_PROTECT", 10)
        monkeypatch.setattr(compaction, "PRUNE_MINIMUM", 10)

        msgs = _make_multiturn_messages([
            ("tc1", LARGE_OUTPUT),  # turn 1 → turns_seen=2 when walking backwards
            ("tc2", LARGE_OUTPUT),  # turn 2 → turns_seen=1 → protected
        ])
        # Structure: Hu(t1) AI(tc1) Tool(tc1) Hu(t2) AI(tc2) Tool(tc2) Hu(t3) AI(reply)
        # Walking backwards: turns_seen=0 for AI(reply), turns_seen=1 at Hu(t3), then tc2 is turns_seen=1

        svc = CompactionService()
        svc.prune(msgs)

        # tc2 (index 5) is turns_seen=1 → PROTECTED, full content
        assert msgs[5].content == LARGE_OUTPUT
        # tc1 (index 2) is turns_seen=2 → can be truncated
        assert TRUNCATED_PREFIX in msgs[2].content
        assert "Tool output truncated" in msgs[2].content

    def test_current_turn_tool_output_always_protected(self, monkeypatch):
        """ToolMessages in the most recent 2 turns are protected;
        older turns are truncated when over token threshold."""
        monkeypatch.setattr(compaction, "PRUNE_PROTECT", 10)
        monkeypatch.setattr(compaction, "PRUNE_MINIMUM", 10)

        # Structure: Hu(t1) AI Tool Hu(t2) AI Tool Hu(t3) AI Tool Hu(t4) AI(reply)
        # Walking backwards turns_seen: tc3=1, tc2=2, tc1=3
        msgs = _make_multiturn_messages([
            ("tc1", LARGE_OUTPUT),  # turn 1 → turns_seen=3 → truncatable
            ("tc2", LARGE_OUTPUT),  # turn 2 → turns_seen=2 → truncatable
            ("tc3", LARGE_OUTPUT),  # turn 3 → turns_seen=1 → protected
        ])

        svc = CompactionService()
        svc.prune(msgs)

        # tc3 (index 8): turns_seen=1 < 2 → PROTECTED
        assert msgs[8].content == LARGE_OUTPUT
        # tc2 (index 5): turns_seen=2 → NOT protected, truncated
        assert "Tool output truncated" in msgs[5].content
        # tc1 (index 2): turns_seen=3 → NOT protected, truncated
        assert "Tool output truncated" in msgs[2].content

    def test_old_turns_bash_output_truncated(self, monkeypatch):
        """Bash ToolMessages in old turns get truncated."""
        monkeypatch.setattr(compaction, "PRUNE_PROTECT", 10)
        monkeypatch.setattr(compaction, "PRUNE_MINIMUM", 10)

        msgs = _make_multiturn_messages([
            ("tc1", LARGE_OUTPUT),  # turn 1 → turns_seen=3 → truncatable
            ("tc2", "short output"),  # turn 2
            ("tc3", LARGE_OUTPUT),  # turn 3 → turns_seen=1 → protected
        ])

        svc = CompactionService()
        svc.prune(msgs)

        # tc1 (index 2): old turn with large output → truncated
        assert "Tool output truncated" in msgs[2].content
        # tc2 (index 5): old turn but short output (< TOOL_OUTPUT_MAX_CHARS) → NOT truncated
        assert msgs[5].content == "short output"
        # tc3 (index 8): recent turn → protected
        assert msgs[8].content == LARGE_OUTPUT

    def test_bash_output_protected_in_recent_turns(self, monkeypatch):
        """Bash tool (non-file-edit) outputs in recent turns are fully protected."""
        monkeypatch.setattr(compaction, "PRUNE_PROTECT", 10)
        monkeypatch.setattr(compaction, "PRUNE_MINIMUM", 10)

        bash_output = "error: command not found\n" * 200  # > 2000 chars

        msgs = _make_multiturn_messages([
            ("tc1", bash_output),  # turn 1 → turns_seen=2 → truncatable
            ("tc2", bash_output),  # turn 2 → turns_seen=1 → protected
        ])

        svc = CompactionService()
        svc.prune(msgs)

        # tc2: recent turn bash output preserved
        assert msgs[5].content == bash_output
        # tc1: old turn bash output truncated
        assert "Tool output truncated" in msgs[2].content


class TestPruneToolMessageEdgeCases:
    """Edge cases for ToolMessage truncation."""

    def test_protected_agent_tool_never_truncated(self, monkeypatch):
        """Agent tool outputs are never truncated regardless of turn."""
        monkeypatch.setattr(compaction, "PRUNE_PROTECT", 10)
        monkeypatch.setattr(compaction, "PRUNE_MINIMUM", 10)

        # Manually build messages with agent tool (protected)
        messages = [
            HumanMessage(content="turn 1"),
            AIMessage(content="", tool_calls=[{"name": "agent", "args": {}, "id": "ag1", "type": "tool_call"}]),
            ToolMessage(content=LARGE_OUTPUT, tool_call_id="ag1", name="agent"),
            HumanMessage(content="turn 2"),
            AIMessage(content="", tool_calls=[{"name": "agent", "args": {}, "id": "ag2", "type": "tool_call"}]),
            ToolMessage(content=LARGE_OUTPUT, tool_call_id="ag2", name="agent"),
            HumanMessage(content="turn 3 (current)"),
            AIMessage(content="assistant reply", id="ai_final"),
        ]

        svc = CompactionService()
        svc.prune(messages)

        # Both agent ToolMessages should be untouched (PRUNE_PROTECTED_TOOLS)
        assert messages[2].content == LARGE_OUTPUT
        assert messages[5].content == LARGE_OUTPUT

    def test_truncation_message_format(self, monkeypatch):
        """Verify the truncation placeholder includes omitted char count."""
        monkeypatch.setattr(compaction, "PRUNE_PROTECT", 10)
        monkeypatch.setattr(compaction, "PRUNE_MINIMUM", 10)

        output = "y" * 5000
        # Need 2 tool outputs so tc1 is turns_seen=2 (not protected)
        msgs = _make_multiturn_messages([
            ("tc1", output),        # turn 1 → turns_seen=2 → truncatable
            ("tc2", "short ok"),    # turn 2 → turns_seen=1 → protected
        ])

        svc = CompactionService()
        svc.prune(msgs)

        truncated = msgs[2].content  # tc1 at index 2
        assert truncated.startswith(output[:TOOL_OUTPUT_MAX_CHARS])
        assert "[Tool output truncated for context:" in truncated
        assert f"omitted {len(output) - TOOL_OUTPUT_MAX_CHARS} chars" in truncated

    def test_tool_output_below_max_chars_not_truncated(self, monkeypatch):
        """Tool outputs shorter than TOOL_OUTPUT_MAX_CHARS are never truncated,
        even in old turns with accumulated tokens over PRUNE_PROTECT."""
        monkeypatch.setattr(compaction, "PRUNE_PROTECT", 10)
        monkeypatch.setattr(compaction, "PRUNE_MINIMUM", 10)

        short = "short output"
        assert len(short) <= TOOL_OUTPUT_MAX_CHARS

        # Need 2 tool outputs so tc1 is turns_seen=2 (old turn, can truncate)
        # but its output is short so it passes the len check
        msgs = _make_multiturn_messages([
            ("tc1", short),         # turn 1 → turns_seen=2, but short → not truncated
            ("tc2", LARGE_OUTPUT),  # turn 2 → turns_seen=1 → protected
        ])

        svc = CompactionService()
        svc.prune(msgs)

        # tc1: old turn but content shorter than TOOL_OUTPUT_MAX_CHARS → not truncated
        assert msgs[2].content == short
        # tc2: recent turn → protected
        assert msgs[5].content == LARGE_OUTPUT
