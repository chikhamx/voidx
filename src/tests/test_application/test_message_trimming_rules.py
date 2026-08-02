"""Tests for message trimming pure functions (rules 1 & 2 helpers)."""

from voidx.agent.application.message_trimming import (
    parse_read_line_range,
    parse_diff_hunk_ranges,
    merge_ranges,
    coverage_ratio,
    build_diff_spans_from_text,
    summarize_edit_diff,
)


# ---------------------------------------------------------------------------
# Helpers for building message sequences
# ---------------------------------------------------------------------------

from langchain_core.messages import AIMessage, ToolMessage, HumanMessage

from voidx.agent.application.message_trimming import trim_superseded_file_tools


def _read_ai(call_id: str, file_path: str, content: str) -> AIMessage:
    """AIMessage with a read tool_call. content is the ToolMessage output."""
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": "read", "args": {"file_path": file_path}, "type": "tool_call"}],
    )


def _read_tool(call_id: str, content: str) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id, status="success")


def _numbered_lines(start: int, end: int) -> str:
    return "\n".join(f"{i}\tline {i}" for i in range(start, end + 1))


def _edit_ai(call_id: str, file_path: str, new_string: str = "x") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": "replace", "args": {"file_path": file_path, "new_string": new_string}, "type": "tool_call"}],
    )


def _edit_tool(call_id: str, diff_content: str) -> ToolMessage:
    return ToolMessage(content=diff_content, tool_call_id=call_id, status="success")


def _tool_call_ids(msg: AIMessage) -> set[str]:
    return {tc["id"] for tc in msg.tool_calls}


def _tool_message_ids(messages: list) -> set[str]:
    return {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}


# ---------------------------------------------------------------------------
# Window bounds & pairing index
# ---------------------------------------------------------------------------

class TestWindowBounds:
    def test_short_history_all_in_window(self):
        msgs = [_read_ai("c1", "f.py", "x"), _read_tool("c1", "1\ta")]
        result = trim_superseded_file_tools(msgs, window_lines=2000)
        # nothing trimmed (only one read)
        assert len(result) == 2

    def test_window_boundary_keeps_aimessage_toolmessage_pair(self):
        # Build history > window_lines so boundary falls somewhere.
        # Each read pair ~ 50 lines. 2000/50 ≈ 40 pairs.
        msgs = []
        for i in range(60):
            cid = f"c{i}"
            msgs.append(_read_ai(cid, f"f{i}.py", "x"))
            msgs.append(_read_tool(cid, _numbered_lines(1, 50)))
        result = trim_superseded_file_tools(msgs, window_lines=2000)
        # Every AIMessage in result must have its ToolMessage present (no orphan).
        for m in result:
            if isinstance(m, AIMessage) and m.tool_calls:
                ids = _tool_call_ids(m)
                tool_ids = _tool_message_ids(result)
                assert ids <= tool_ids, f"AIMessage tool_calls {ids} missing ToolMessage"


class TestPairingIndex:
    def test_unpaired_tool_message_kept(self):
        # ToolMessage without matching AIMessage tool_call → kept as-is.
        msgs = [HumanMessage(content="hi"), _read_tool("orphan", "1\ta")]
        result = trim_superseded_file_tools(msgs, window_lines=2000)
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == "orphan" for m in result)

    def test_error_tool_message_not_trimmed(self):
        # Error status ToolMessage should not enter trimming.
        ai = _read_ai("c1", "f.py", "x")
        tool = ToolMessage(content="error", tool_call_id="c1", status="error")
        result = trim_superseded_file_tools([ai, tool], window_lines=2000)
        assert any(m.tool_call_id == "c1" for m in result if isinstance(m, ToolMessage))


# ---------------------------------------------------------------------------
# Rule 1: superseded read deletion
# ---------------------------------------------------------------------------

class TestRule1SupersededRead:
    def test_scenario1_multiple_overlapping_reads(self):
        """旧: 1-10, 11-20, 21-30; 新: 5-15, 16-26, 27-37 → 旧全删."""
        msgs = []
        # old reads
        for i, (s, e) in enumerate([(1, 10), (11, 20), (21, 30)]):
            cid = f"old{i}"
            msgs.append(_read_ai(cid, "f.py", "x"))
            msgs.append(_read_tool(cid, _numbered_lines(s, e)))
        # new reads
        for i, (s, e) in enumerate([(5, 15), (16, 26), (27, 37)]):
            cid = f"new{i}"
            msgs.append(_read_ai(cid, "f.py", "x"))
            msgs.append(_read_tool(cid, _numbered_lines(s, e)))
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        result_ids = _tool_message_ids(result)
        # old reads deleted
        assert "old0" not in result_ids
        assert "old1" not in result_ids
        assert "old2" not in result_ids
        # new reads kept
        assert "new0" in result_ids
        assert "new1" in result_ids
        assert "new2" in result_ids

    def test_scenario2_large_read_then_small_reads(self):
        """旧: 1-100; 新: 10-30, 40-70, 71-100 → 旧在第三条后删."""
        msgs = []
        msgs.append(_read_ai("old", "f.py", "x"))
        msgs.append(_read_tool("old", _numbered_lines(1, 100)))
        for i, (s, e) in enumerate([(10, 30), (40, 70), (71, 100)]):
            cid = f"new{i}"
            msgs.append(_read_ai(cid, "f.py", "x"))
            msgs.append(_read_tool(cid, _numbered_lines(s, e)))
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        result_ids = _tool_message_ids(result)
        assert "old" not in result_ids
        for i in range(3):
            assert f"new{i}" in result_ids

    def test_below_threshold_not_deleted(self):
        """旧: 1-100; 新: 10-30 (21%) → 旧保留."""
        msgs = [
            _read_ai("old", "f.py", "x"),
            _read_tool("old", _numbered_lines(1, 100)),
            _read_ai("new", "f.py", "x"),
            _read_tool("new", _numbered_lines(10, 30)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        assert "old" in _tool_message_ids(result)

    def test_new_read_not_deleted_by_old_large_read(self):
        """旧: 1-100; 新: 20-30 → 新必须保留 (不能用旧作为覆盖证据)."""
        msgs = [
            _read_ai("old", "f.py", "x"),
            _read_tool("old", _numbered_lines(1, 100)),
            _read_ai("new", "f.py", "x"),
            _read_tool("new", _numbered_lines(20, 30)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        result_ids = _tool_message_ids(result)
        assert "new" in result_ids

    def test_union_excludes_deleted_read_exclusive_range(self):
        """read A(1-100) → B(50-80) → C(60-70) → D(50-60).
        B 被删后 B 的独占区间(71-80)不再算入 union."""
        msgs = [
            _read_ai("A", "f.py", "x"), _read_tool("A", _numbered_lines(1, 100)),
            _read_ai("B", "f.py", "x"), _read_tool("B", _numbered_lines(50, 80)),
            _read_ai("C", "f.py", "x"), _read_tool("C", _numbered_lines(60, 70)),
            _read_ai("D", "f.py", "x"), _read_tool("D", _numbered_lines(50, 60)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        result_ids = _tool_message_ids(result)
        # A should be deleted (covered by B+C+D union 50-80 → 31/100 = 31%? no)
        # Actually A(1-100) covered by union of B,C,D = (50-80) = 31 lines = 31% < 60% → A kept
        assert "A" in result_ids


# ---------------------------------------------------------------------------
# Rule 1: edit remap (small edit doesn't kill large read)
# ---------------------------------------------------------------------------

class TestRule1EditRemap:
    def test_small_edit_does_not_delete_large_read(self):
        """read 1-100 → edit 20-25 (净减2) → read 1-20.
        edit 时只 remap 不删除; 新 read 覆盖率 <60% → 旧 read 保留."""
        old_content = "\n".join(f"line {i}" for i in range(1, 101))
        new_content = old_content  # edit doesn't change net lines much
        # diff replacing 20-25 with 20-23 (net -2)
        diff = (
            "File edited: f.py (1 operations)\n"
            "@@ -20,6 +20,4 @@\n"
            " ctx\n"
            "-old\n"
            "-old\n"
            "+new\n"
            " ctx\n"
        )
        msgs = [
            _read_ai("old", "f.py", "x"), _read_tool("old", _numbered_lines(1, 100)),
            _edit_ai("e1", "f.py"), _edit_tool("e1", diff),
            _read_ai("new", "f.py", "x"), _read_tool("new", _numbered_lines(1, 20)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        # old read should survive (coverage of remapped range < 60%)
        assert "old" in _tool_message_ids(result)

    def test_edit_remap_empty_range_deletes_read(self):
        """read 1-10 → edit deletes lines 1-10 entirely → old read deleted."""
        diff = (
            "File edited: f.py (1 operations)\n"
            "@@ -1,10 +1,0 @@\n"
            "-line 1\n-line 2\n-line 3\n-line 4\n-line 5\n-line 6\n-line 7\n-line 8\n-line 9\n-line 10\n"
        )
        msgs = [
            _read_ai("old", "f.py", "x"), _read_tool("old", _numbered_lines(1, 10)),
            _edit_ai("e1", "f.py"), _edit_tool("e1", diff),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        assert "old" not in _tool_message_ids(result)

    def test_second_edit_empties_first_edit_hunk_ranges(self):
        """fb2: edit1 (hunk 20-25) → edit2 deletes lines 20-25 entirely.
        edit1's hunk_ranges should be cleared (not kept stale), so rule 2
        coverage on edit1 uses empty ranges → not summarized."""
        diff1 = (
            "File edited: f.py (1 operations)\n"
            "@@ -20,3 +20,3 @@\n ctx\n-old\n+new\n ctx\n"
        )
        diff2 = (
            "File edited: f.py (1 operations)\n"
            "@@ -20,3 +20,0 @@\n-line 20\n-line 21\n-line 22\n"
        )
        msgs = [
            _edit_ai("e1", "f.py"), _edit_tool("e1", diff1),
            _edit_ai("e2", "f.py"), _edit_tool("e2", diff2),
            _read_ai("r1", "f.py", "x"), _read_tool("r1", _numbered_lines(1, 50)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        # e1's hunk_ranges remapped to empty → coverage_ratio([], union) == 0
        # → e1 not summarized (diff kept intact)
        for m in result:
            if isinstance(m, ToolMessage) and m.tool_call_id == "e1":
                assert "@@" in m.content
                assert "Changed lines:" not in m.content
                return
        assert False, "e1 ToolMessage not found"


# ---------------------------------------------------------------------------
# Multi tool_call partial deletion & raw provider cleanup
# ---------------------------------------------------------------------------

class TestMultiToolCallPartialDeletion:
    def test_partial_deletion_keeps_other_tool_call(self):
        """AIMessage with read foo(1-100) + read bar(1-20); later read covers foo only.
        Only foo deleted, bar kept; AIMessage retained (has remaining tool_call)."""
        ai = AIMessage(content="", tool_calls=[
            {"id": "foo", "name": "read", "args": {"file_path": "foo.py"}, "type": "tool_call"},
            {"id": "bar", "name": "read", "args": {"file_path": "bar.py"}, "type": "tool_call"},
        ])
        msgs = [
            ai,
            _read_tool("foo", _numbered_lines(1, 100)),
            _read_tool("bar", _numbered_lines(1, 20)),
            _read_ai("new", "foo.py", "x"),
            _read_tool("new", _numbered_lines(1, 100)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        result_ids = _tool_message_ids(result)
        assert "foo" not in result_ids
        assert "bar" in result_ids
        # AIMessage retained with only bar tool_call
        ai_msgs = [m for m in result if isinstance(m, AIMessage) and m.tool_calls]
        assert any(_tool_call_ids(m) == {"bar"} for m in ai_msgs)

    def test_empty_aimessage_after_full_removal_deleted(self):
        """AIMessage with single read tool_call, text content empty, tool_call deleted → AIMessage removed."""
        msgs = [
            _read_ai("old", "f.py", "x"),
            _read_tool("old", _numbered_lines(1, 100)),
            _read_ai("new", "f.py", "x"),
            _read_tool("new", _numbered_lines(1, 100)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        # old AIMessage (empty content, single tool_call) should be removed
        ai_contents = [m.content for m in result if isinstance(m, AIMessage)]
        # only the "new" AIMessage remains
        assert len([c for c in ai_contents if c == ""]) == 1

    def test_aimessage_with_text_kept_after_tool_call_removal(self):
        """AIMessage has text content + read tool_call; tool_call deleted → AIMessage kept (text only)."""
        ai = AIMessage(
            content="Let me read this file",
            tool_calls=[{"id": "old", "name": "read", "args": {"file_path": "f.py"}, "type": "tool_call"}],
        )
        msgs = [
            ai,
            _read_tool("old", _numbered_lines(1, 100)),
            _read_ai("new", "f.py", "x"),
            _read_tool("new", _numbered_lines(1, 100)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        # AIMessage with text kept, but tool_calls empty
        text_ai = [m for m in result if isinstance(m, AIMessage) and m.content == "Let me read this file"]
        assert len(text_ai) == 1
        assert text_ai[0].tool_calls == []


class TestRawProviderToolCallCleanup:
    def test_additional_kwargs_synced(self):
        """AIMessage with tool_calls + additional_kwargs[tool_calls]; deletion syncs both."""
        ai = AIMessage(
            content="",
            tool_calls=[{"id": "old", "name": "read", "args": {"file_path": "f.py"}, "type": "tool_call"}],
            additional_kwargs={"tool_calls": [{"id": "old", "name": "read", "args": {"file_path": "f.py"}}]},
        )
        msgs = [
            ai,
            _read_tool("old", _numbered_lines(1, 100)),
            _read_ai("new", "f.py", "x"),
            _read_tool("new", _numbered_lines(1, 100)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        for m in result:
            if isinstance(m, AIMessage):
                raw = (m.additional_kwargs or {}).get("tool_calls", [])
                assert not any(rc.get("id") == "old" for rc in raw)

    def test_content_list_tool_use_synced(self):
        """AIMessage content list with raw tool_use block; deletion syncs content list."""
        ai = AIMessage(
            content=[{"type": "text", "text": "thinking"}, {"type": "tool_use", "id": "old", "name": "read", "input": {"file_path": "f.py"}}],
            tool_calls=[{"id": "old", "name": "read", "args": {"file_path": "f.py"}, "type": "tool_call"}],
        )
        msgs = [
            ai,
            _read_tool("old", _numbered_lines(1, 100)),
            _read_ai("new", "f.py", "x"),
            _read_tool("new", _numbered_lines(1, 100)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        for m in result:
            if isinstance(m, AIMessage) and isinstance(m.content, list):
                for block in m.content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        assert block.get("id") != "old"

    def test_raw_only_content_list_aimessage_dropped(self):
        """P2: AIMessage with only a raw tool_use block (no text) → dropped
        after deletion, not kept as empty content=[] AIMessage."""
        ai = AIMessage(
            content=[{"type": "tool_use", "id": "old", "name": "read", "input": {"file_path": "f.py"}}],
            tool_calls=[{"id": "old", "name": "read", "args": {"file_path": "f.py"}, "type": "tool_call"}],
        )
        msgs = [
            ai,
            _read_tool("old", _numbered_lines(1, 100)),
            _read_ai("new", "f.py", "x"),
            _read_tool("new", _numbered_lines(1, 100)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        # The raw-only AIMessage should be dropped entirely (no empty survivor)
        ai_msgs = [m for m in result if isinstance(m, AIMessage)]
        assert all(m.content != [] or m.tool_calls for m in ai_msgs)

    def test_raw_only_additional_kwargs_indexed(self):
        """P3: tool call only in additional_kwargs (not in tool_calls) is indexed."""
        ai = AIMessage(
            content="",
            tool_calls=[],
            additional_kwargs={"tool_calls": [{"id": "old", "name": "read", "args": {"file_path": "f.py"}}]},
        )
        msgs = [
            ai,
            _read_tool("old", _numbered_lines(1, 100)),
            _read_ai("new", "f.py", "x"),
            _read_tool("new", _numbered_lines(1, 100)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        result_ids = _tool_message_ids(result)
        assert "old" not in result_ids
        assert "new" in result_ids

    def test_raw_only_content_list_indexed(self):
        """P3: tool call only in content-list raw tool_use (not in tool_calls) is indexed."""
        ai = AIMessage(
            content=[{"type": "tool_use", "id": "old", "name": "read", "input": {"file_path": "f.py"}}],
            tool_calls=[],
        )
        msgs = [
            ai,
            _read_tool("old", _numbered_lines(1, 100)),
            _read_ai("new", "f.py", "x"),
            _read_tool("new", _numbered_lines(1, 100)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        result_ids = _tool_message_ids(result)
        assert "old" not in result_ids
        assert "new" in result_ids


# ---------------------------------------------------------------------------
# Rule 2: edit diff summarization
# ---------------------------------------------------------------------------

class TestRule2EditDiffSummarization:
    def test_edit_diff_summarized_when_covered_by_new_read(self):
        """edit (hunk 20-31) → read (1-50) → edit diff summarized."""
        diff = (
            "File edited: f.py (1 operations)\n"
            "@@ -20,7 +20,12 @@\n"
            " context\n"
            "-old\n"
            "+new line 1\n+new line 2\n+new line 3\n+new line 4\n+new line 5\n+new line 6\n"
            " context\n"
        )
        msgs = [
            _edit_ai("e1", "f.py"), _edit_tool("e1", diff),
            _read_ai("new", "f.py", "x"), _read_tool("new", _numbered_lines(1, 50)),
        ]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        for m in result:
            if isinstance(m, ToolMessage) and m.tool_call_id == "e1":
                assert "Changed lines: 20-31" in m.content
                assert "@@" not in m.content
                assert "+new line" not in m.content
                return
        assert False, "edit ToolMessage not found"

    def test_edit_diff_not_summarized_without_covering_read(self):
        """edit without subsequent read → diff kept intact."""
        diff = (
            "File edited: f.py (1 operations)\n"
            "@@ -20,7 +20,12 @@\n ctx\n-old\n+new\n ctx\n"
        )
        msgs = [_edit_ai("e1", "f.py"), _edit_tool("e1", diff)]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        for m in result:
            if isinstance(m, ToolMessage) and m.tool_call_id == "e1":
                assert "@@" in m.content
                assert "+new" in m.content
                return
        assert False, "edit ToolMessage not found"

    def test_manage_tool_not_summarized(self):
        """manage create/delete/move diff not summarized (rule 2 excludes manage tool)."""
        from langchain_core.messages import AIMessage
        ai = AIMessage(
            content="",
            tool_calls=[{"id": "f1", "name": "manage", "args": {"op": "create", "paths": "f.py"}, "type": "tool_call"}],
        )
        tool = ToolMessage(
            content="File created: f.py\n@@ -0,0 +1,5 @@\n+line 1\n+line 2\n+line 3\n+line 4\n+line 5\n",
            tool_call_id="f1",
            status="success",
        )
        read_ai = _read_ai("r1", "f.py", "x")
        read_tool = _read_tool("r1", _numbered_lines(1, 50))
        msgs = [ai, tool, read_ai, read_tool]
        result = trim_superseded_file_tools(msgs, window_lines=20000)
        for m in result:
            if isinstance(m, ToolMessage) and m.tool_call_id == "f1":
                assert "File created: f.py" in m.content
                assert "@@" in m.content
                assert "+line 1" in m.content
                return
        assert False, "file ToolMessage not found"


# ---------------------------------------------------------------------------
# Window outside not trimmed
# ---------------------------------------------------------------------------

class TestWindowOutsideNotTrimmed:
    def test_read_outside_window_not_trimmed(self):
        """read A → (大量 message 填满窗口) → read B; A 在窗口外不被处理."""
        msgs = [_read_ai("A", "f.py", "x"), _read_tool("A", _numbered_lines(1, 100))]
        # fill window with unrelated reads
        for i in range(50):
            cid = f"fill{i}"
            msgs.append(_read_ai(cid, f"other{i}.py", "x"))
            msgs.append(_read_tool(cid, _numbered_lines(1, 50)))
        # new read of f.py at the end
        msgs.append(_read_ai("B", "f.py", "x"))
        msgs.append(_read_tool("B", _numbered_lines(1, 100)))
        result = trim_superseded_file_tools(msgs, window_lines=2000)
        # A is outside window, should still be present
        assert "A" in _tool_message_ids(result)
