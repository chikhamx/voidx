"""Tests for file edit operations — replace and line insert via registry."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry
import voidx.tools.file_state as file_state

class TestFileOps:
    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_sequential_replace_with_multiple_covered_ranges(self, tmp_path):
        f = tmp_path / "batch.txt"
        f.write_text("one\ntwo\nthree\nfour\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "batch.txt"}, ctx)

        r1 = await r.execute_tool(
            "replace",
            {"file_path": "batch.txt", "start_no": 1, "end_no": 1, "start_anchor": "one", "end_anchor": "one", "new_string": "ONE"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "batch.txt", "start_no": 3, "end_no": 4, "start_anchor": "three", "end_anchor": "four", "new_string": "THREE\nFOUR\n"},
            ctx,
        )

        assert r1.metadata.get("error") is not True
        assert r2.metadata.get("error") is not True
        assert (tmp_path / "batch.txt").read_text() == "ONE\ntwo\nTHREE\nFOUR\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_insert_uses_line_number_and_content_only(self, tmp_path):
        f = tmp_path / "insert-tool.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "insert-tool.txt", "offset": 1, "limit": 2}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "insert-tool.txt", "op": "insert", "lineno": 1, "new_string": "middle\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        # 0-based insert-before: lineno=1 means insert before line 2 (1-based)
        assert (tmp_path / "insert-tool.txt").read_text() == "one\nmiddle\ntwo\n"

    @pytest.mark.asyncio
    async def test_line_insert_requires_read_coverage_for_target_line(self, tmp_path):
        f = tmp_path / "insert-unread.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "insert-unread.txt", "op": "insert", "lineno": 1, "new_string": "middle\n"},
            ctx,
        )

        assert "read" in result.output.lower()
        assert result.metadata.get("error")
        assert f.read_text() == "one\ntwo\n"

    @pytest.mark.asyncio
    async def test_line_insert_at_beginning_of_file(self, tmp_path):
        f = tmp_path / "insert-bof.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "insert-bof.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "insert-bof.txt", "op": "insert", "lineno": 0, "new_string": "zero\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "zero\none\ntwo\n"

    @pytest.mark.asyncio
    async def test_replace_tool_replaces_whole_line_range_near_start_and_end(self, tmp_path):
        f = tmp_path / "replace-tool.txt"
        f.write_text("one\ntwo = 2\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "replace-tool.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "replace-tool.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "two",
                "end_anchor": "two",
                "new_string": "TWO",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\nTWO\nthree\n"

    @pytest.mark.asyncio
    async def test_replace_selects_best_valid_line_range_pair(self, tmp_path):
        f = tmp_path / "pair-score.txt"
        f.write_text(
            "preamble\n"
            "target start decoy\n"
            "noise\n"
            "target end decoy\n"
            "middle\n"
            "target start real\n"
            "body\n"
            "target end real\n"
            "tail\n"
        )
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "pair-score.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "pair-score.txt",
                "start_no": 6,
                "end_no": 8,
                "start_anchor": "target start",
                "end_anchor": "target end",
                "new_string": "replacement\nblock\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 6
        assert result.metadata["end_line"] == 8
        assert f.read_text() == (
            "preamble\n"
            "target start decoy\n"
            "noise\n"
            "target end decoy\n"
            "middle\n"
            "replacement\n"
            "block\n"
            "tail\n"
        )

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_sequential_replace_on_adjacent_lines(self, tmp_path):
        f = tmp_path / "overlap.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "overlap.txt"}, ctx)

        r1 = await r.execute_tool(
            "replace",
            {"file_path": "overlap.txt", "start_no": 1, "end_no": 1, "start_anchor": "one", "end_anchor": "one", "new_string": "x"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "overlap.txt", "start_no": 2, "end_no": 2, "start_anchor": "two", "end_anchor": "two", "new_string": "y"},
            ctx,
        )

        assert r1.metadata.get("error") is not True
        assert r2.metadata.get("error") is not True
        assert (tmp_path / "overlap.txt").read_text() == "x\ny\nthree\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_preserves_read_coverage_after_success(self, tmp_path):
        f = tmp_path / "coverage.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "coverage.txt"}, ctx)
        first = await r.execute_tool(
            "replace",
            {"file_path": "coverage.txt", "start_no": 1, "end_no": 1, "start_anchor": "one", "end_anchor": "one", "new_string": "ONE"},
            ctx,
        )

        second = await r.execute_tool(
            "replace",
            {"file_path": "coverage.txt", "start_no": 2, "end_no": 2, "start_anchor": "two", "end_anchor": "two", "new_string": "TWO"},
            ctx,
        )

        assert first.metadata.get("error") is not True
        assert second.metadata.get("error") is not True
        assert (tmp_path / "coverage.txt").read_text() == "ONE\nTWO\n"

    @pytest.mark.asyncio
    async def test_replace_does_not_mark_unseen_lines_as_read_after_partial_edit(self, tmp_path):
        f = tmp_path / "partial-coverage.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 13)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "partial-coverage.txt", "offset": 1, "limit": 2}, ctx)

        edit = await r.execute_tool(
            "replace",
            {"file_path": "partial-coverage.txt", "start_no": 2, "end_no": 2, "start_anchor": "line 2", "end_anchor": "line 2", "new_string": "LINE 2"},
            ctx,
        )
        reread = await r.execute_tool("read", {"file_path": "partial-coverage.txt", "offset": 10, "limit": 1}, ctx)

        assert edit.metadata.get("error") is not True
        assert reread.metadata.get("already_read") is not True
        assert "10\tline 10" in reread.output

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_expand_remaps_read_coverage_precisely(self, tmp_path):
        f = tmp_path / "expand-coverage.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 41)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "expand-coverage.txt", "offset": 1, "limit": 30}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "expand-coverage.txt", "start_no": 5, "end_no": 5, "start_anchor": "line 5", "end_anchor": "line 5", "new_string": "line 5a\nline 5b"},
            ctx,
        )
        reread = await r.execute_tool("read", {"file_path": "expand-coverage.txt", "offset": 32, "limit": 1}, ctx)

        assert result.metadata.get("error") is not True
        assert file_state.covered_read_range(ctx, f, 1, 31) is not None
        assert reread.metadata.get("already_read") is not True
        assert "32\tline 31" in reread.output

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_delete_remaps_read_coverage_precisely(self, tmp_path):
        f = tmp_path / "delete-coverage.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "delete-coverage.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "delete-coverage.txt", "start_no": 50, "end_no": 50, "start_anchor": "line 50", "end_anchor": "line 50", "new_string": ""},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert file_state.covered_read_range(ctx, f, 1, 99) is not None

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_read_same_line_after_diff_is_already_read(self, tmp_path):
        f = tmp_path / "same-line.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "same-line.txt", "offset": 1, "limit": 2}, ctx)

        edit = await r.execute_tool(
            "replace",
            {"file_path": "same-line.txt", "start_no": 2, "end_no": 2, "start_anchor": "two", "end_anchor": "two", "new_string": "TWO"},
            ctx,
        )
        reread = await r.execute_tool("read", {"file_path": "same-line.txt", "offset": 2, "limit": 1}, ctx)

        assert edit.metadata.get("error") is not True
        assert reread.metadata.get("already_read")

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_sequential_replace_remaps_and_merges_coverage(self, tmp_path):
        f = tmp_path / "multi-hunk-coverage.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-hunk-coverage.txt"}, ctx)

        r1 = await r.execute_tool(
            "replace",
            {"file_path": "multi-hunk-coverage.txt", "start_no": 10, "end_no": 10, "start_anchor": "line 10", "end_anchor": "line 10", "new_string": "line 10a\nline 10b"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "multi-hunk-coverage.txt", "start_no": 51, "end_no": 51, "start_anchor": "line 50", "end_anchor": "line 50", "new_string": ""},
            ctx,
        )

        assert r1.metadata.get("error") is not True
        assert r2.metadata.get("error") is not True
        assert file_state.covered_read_range(ctx, f, 1, 100) is not None

    @pytest.mark.asyncio
    async def test_replace_noop_refreshes_read_coverage_fingerprint(self, tmp_path):
        f = tmp_path / "noop-coverage.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "noop-coverage.txt", "offset": 1, "limit": 1}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "noop-coverage.txt", "start_no": 1, "end_no": 1, "start_anchor": "one", "end_anchor": "one", "new_string": "one"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert file_state.covered_read_range(ctx, f, 1, 1) is not None

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_after_partial_edit_still_rejects_unread_target(self, tmp_path):
        f = tmp_path / "partial-edit.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 13)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "partial-edit.txt", "offset": 1, "limit": 2}, ctx)

        first = await r.execute_tool(
            "replace",
            {"file_path": "partial-edit.txt", "start_no": 2, "end_no": 2, "start_anchor": "line 2", "end_anchor": "line 2", "new_string": "LINE 2"},
            ctx,
        )
        second = await r.execute_tool(
            "replace",
            {"file_path": "partial-edit.txt", "start_no": 10, "end_no": 10, "start_anchor": "line 10", "end_anchor": "line 10", "new_string": "LINE 10"},
            ctx,
        )

        assert first.metadata.get("error") is not True
        assert "read" in second.output.lower()
        assert second.metadata.get("error")
        assert (tmp_path / "partial-edit.txt").read_text().splitlines()[9] == "line 10"

    @pytest.mark.asyncio
    async def test_merge_overlapping_read_ranges(self, tmp_path):
        f = tmp_path / "merge.txt"
        f.write_text("\n".join(str(i) for i in range(1, 101)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "merge.txt", "offset": 1, "limit": 50}, ctx)
        await r.execute_tool("read", {"file_path": "merge.txt", "offset": 40, "limit": 61}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "merge.txt", "start_no": 1, "end_no": 1, "start_anchor": "1", "end_anchor": "1", "new_string": "CHANGED"},
            ctx,
        )
        assert result.metadata.get("error") is not True

    @pytest.mark.asyncio
    async def test_merge_adjacent_read_ranges(self, tmp_path):
        f = tmp_path / "adjacent.txt"
        f.write_text("\n".join(str(i) for i in range(1, 101)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "adjacent.txt", "offset": 1, "limit": 50}, ctx)
        await r.execute_tool("read", {"file_path": "adjacent.txt", "offset": 51, "limit": 50}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "adjacent.txt", "start_no": 50, "end_no": 51, "start_anchor": "50", "end_anchor": "51", "new_string": "MERGED"},
            ctx,
        )
        assert result.metadata.get("error") is not True

    @pytest.mark.asyncio
    async def test_non_adjacent_ranges_not_covered(self, tmp_path):
        f = tmp_path / "gap.txt"
        f.write_text("\n".join(str(i) for i in range(1, 31)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "gap.txt", "offset": 1, "limit": 10}, ctx)
        await r.execute_tool("read", {"file_path": "gap.txt", "offset": 20, "limit": 11}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "gap.txt", "start_no": 15, "end_no": 15, "start_anchor": "15", "end_anchor": "15", "new_string": "GAP"},
            ctx,
        )
        assert "read" in result.output.lower()
        assert result.metadata.get("error")

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_insert_lineno_out_of_range_gives_friendly_error(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "short.txt", "op": "insert", "lineno": 5, "new_string": "oops\n"},
            ctx,
        )

        assert result.metadata.get("error")
        assert "insert" in result.output.lower() or "line" in result.output.lower()

    @pytest.mark.asyncio
    async def test_line_insert_into_empty_file_at_lineno_0(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "empty.txt", "op": "insert", "lineno": 0, "new_string": "first\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "first\n"

    @pytest.mark.asyncio
    async def test_line_insert_at_end_of_file(self, tmp_path):
        f = tmp_path / "end.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "end.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "end.txt", "op": "insert", "lineno": 2, "new_string": "three\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\ntwo\nthree\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_append_at_end_of_file(self, tmp_path):
        f = tmp_path / "end.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "end.txt", "op": "append", "new_string": "three\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\ntwo\nthree\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_append_into_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "empty.txt", "op": "append", "new_string": "first\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "first\n"

    @pytest.mark.asyncio
    async def test_line_insert_remaps_read_coverage(self, tmp_path):
        f = tmp_path / "remap.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 11)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "remap.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "remap.txt", "op": "insert", "lineno": 3, "new_string": "inserted\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        reread = await r.execute_tool("read", {"file_path": "remap.txt", "offset": 11, "limit": 1}, ctx)
        assert reread.metadata.get("already_read")

    @pytest.mark.asyncio
    async def test_replace_deletes_text_segment_with_empty_new_string(self, tmp_path):
        f = tmp_path / "delete.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "delete.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "delete.txt", "start_no": 2, "end_no": 2, "start_anchor": "two", "end_anchor": "two", "new_string": ""},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\nthree\n"

    @pytest.mark.asyncio
    async def test_replace_multiline_text_segment(self, tmp_path):
        f = tmp_path / "multi.txt"
        f.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "multi.txt", "start_no": 1, "end_no": 2, "start_anchor": "def foo():", "end_anchor": "pass", "new_string": "def foo():\n    return 42"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "def foo():\n    return 42\n\ndef bar():\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_rejects_text_outside_three_line_window(self, tmp_path):
        f = tmp_path / "window.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 80)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "window.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "window.txt", "start_no": 1, "end_no": 1, "start_anchor": "line 40", "end_anchor": "line 40", "new_string": "LINE 40"},
            ctx,
        )

        assert result.metadata.get("error")
        assert "3" in result.output
        assert "line 40" in f.read_text()

    @pytest.mark.asyncio
    async def test_replace_rejects_nonexistent_prefix(self, tmp_path):
        f = tmp_path / "nope.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "nope.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "nope.txt", "start_no": 1, "end_no": 1, "start_anchor": "nonexistent", "end_anchor": "nonexistent", "new_string": "X"},
            ctx,
        )

        assert result.metadata.get("error")
        assert f.read_text() == "one\ntwo\n"

    @pytest.mark.asyncio
    async def test_replace_rejects_ambiguous_text_segment_in_window(self, tmp_path):
        f = tmp_path / "ambiguous.txt"
        f.write_text("target\nmiddle\ntarget\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "ambiguous.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "ambiguous.txt", "start_no": 2, "end_no": 2, "start_anchor": "target", "end_anchor": "target", "new_string": "TARGET"},
            ctx,
        )

        assert result.metadata.get("error")
        assert "ambiguous" in result.output.lower()
        assert f.read_text() == "target\nmiddle\ntarget\n"

    @pytest.mark.asyncio
    async def test_replace_rejects_suffix_match_on_wrong_declared_end_line(self, tmp_path):
        f = tmp_path / "wrong-end.txt"
        f.write_text("hello world\nfoo bar\nbaz\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "wrong-end.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "wrong-end.txt",
                "start_no": 1,
                "end_no": 3,
                "start_anchor": "hello",
                "end_anchor": "world",
                "new_string": "replacement\n",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "no valid replace range" in result.output
        assert f.read_text() == "hello world\nfoo bar\nbaz\n"

    @pytest.mark.asyncio
    async def test_replace_allows_empty_prefix_for_empty_start_line(self, tmp_path):
        f = tmp_path / "empty-start.txt"
        f.write_text("top\n\nbody\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "empty-start.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-start.txt",
                "start_no": 2,
                "end_no": 3,
                "start_anchor": "",
                "end_anchor": "body",
                "new_string": "replacement\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 3
        assert f.read_text() == "top\nreplacement\nend\n"

    @pytest.mark.asyncio
    async def test_replace_allows_empty_suffix_for_empty_end_line(self, tmp_path):
        f = tmp_path / "empty-end.txt"
        f.write_text("top\nbody\n\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "empty-end.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-end.txt",
                "start_no": 2,
                "end_no": 3,
                "start_anchor": "body",
                "end_anchor": "",
                "new_string": "replacement\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 3
        assert f.read_text() == "top\nreplacement\nend\n"

    @pytest.mark.asyncio
    async def test_replace_empty_anchor_does_not_match_non_empty_line(self, tmp_path):
        f = tmp_path / "empty-anchor-missing.txt"
        f.write_text("top\nbody\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "empty-anchor-missing.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-anchor-missing.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "",
                "end_anchor": "body",
                "new_string": "replacement\n",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "empty line" in result.output
        assert f.read_text() == "top\nbody\nend\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_does_not_corrupt_next_line(self, tmp_path):
        """Regression: new_string ending with \\n must not insert a blank line before the next line."""
        f = tmp_path / "trailing.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "trailing.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "trailing.txt", "start_no": 2, "end_no": 2, "start_anchor": "line2", "end_anchor": "line2", "new_string": "NEW2\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nNEW2\nline3\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_multiline_does_not_corrupt_next_line(self, tmp_path):
        """Regression: multi-line new_string ending with \\n must not insert blank line."""
        f = tmp_path / "multi-trailing.txt"
        f.write_text("line1\nline2\nline3\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-trailing.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "multi-trailing.txt", "start_no": 2, "end_no": 3, "start_anchor": "line2", "end_anchor": "line3", "new_string": "NEW_A\nNEW_B\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nNEW_A\nNEW_B\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_on_last_line(self, tmp_path):
        """Regression: replacing last line with trailing-newline new_string must not add extra blank line."""
        f = tmp_path / "last-line.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "last-line.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "last-line.txt", "start_no": 3, "end_no": 3, "start_anchor": "line3", "end_anchor": "line3", "new_string": "NEW3\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nline2\nNEW3\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_no_trailing_newline_file(self, tmp_path):
        """File without trailing newline: new_string ending with \\n must not add extra blank line."""
        f = tmp_path / "no-trailing.txt"
        f.write_text("line1\nline2\nline3")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "no-trailing.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "no-trailing.txt", "start_no": 2, "end_no": 2, "start_anchor": "line2", "end_anchor": "line2", "new_string": "NEW2\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nNEW2\nline3"

    @pytest.mark.asyncio
    async def test_replace_no_trailing_newline_preserves_next_line(self, tmp_path):
        """new_string without trailing \\n: next line must be untouched (baseline)."""
        f = tmp_path / "baseline.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "baseline.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "baseline.txt", "start_no": 2, "end_no": 2, "start_anchor": "line2", "end_anchor": "line2", "new_string": "NEW2"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nNEW2\nline3\n"

    @pytest.mark.asyncio
    async def test_replace_double_trailing_newline_intentional_blank_line(self, tmp_path):
        """new_string ending with \\n\\n (intentional blank line after replacement)."""
        f = tmp_path / "double-newline.txt"
        f.write_text("line1\nline2\nline3\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "double-newline.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "double-newline.txt", "start_no": 2, "end_no": 2, "start_anchor": "line2", "end_anchor": "line2", "new_string": "NEW2\n\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nNEW2\n\nline3\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_python_code(self, tmp_path):
        """Realistic: replace function body in Python code with trailing newline."""
        f = tmp_path / "code.py"
        f.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "code.py"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "code.py", "start_no": 1, "end_no": 2, "start_anchor": "def foo():", "end_anchor": "return 1", "new_string": "def foo():\n    return 42\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "def foo():\n    return 42\n\ndef bar():\n    return 2\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_multiline_to_single_line(self, tmp_path):
        """Replace multi-line segment with single line ending in \\n."""
        f = tmp_path / "multi-to-single.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-to-single.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "multi-to-single.txt", "start_no": 2, "end_no": 4, "start_anchor": "line2", "end_anchor": "line4", "new_string": "REPLACED\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nREPLACED\nline5\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_single_line_file(self, tmp_path):
        """Single-line file: replace with trailing-newline new_string."""
        f = tmp_path / "single.txt"
        f.write_text("only_line\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "single.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "single.txt", "start_no": 1, "end_no": 1, "start_anchor": "only_line", "end_anchor": "only_line", "new_string": "REPLACED\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "REPLACED\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_two_line_file(self, tmp_path):
        """Two-line file: replace first line with trailing-newline new_string."""
        f = tmp_path / "two.txt"
        f.write_text("line1\nline2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "two.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "two.txt", "start_no": 1, "end_no": 1, "start_anchor": "line1", "end_anchor": "line1", "new_string": "NEW1\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "NEW1\nline2\n"

    @pytest.mark.asyncio
    async def test_replace_sequential_trailing_newline_replaces(self, tmp_path):
        """Two sequential replaces both with trailing newline: no cumulative blank lines."""
        f = tmp_path / "sequential.txt"
        f.write_text("line1\nline2\nline3\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "sequential.txt"}, ctx)

        r1 = await r.execute_tool(
            "replace",
            {"file_path": "sequential.txt", "start_no": 2, "end_no": 2, "start_anchor": "line2", "end_anchor": "line2", "new_string": "NEW2\n"},
            ctx,
        )
        assert r1.metadata.get("error") is not True

        # Read again to update coverage after first edit
        await r.execute_tool("read", {"file_path": "sequential.txt"}, ctx)

        r2 = await r.execute_tool(
            "replace",
            {"file_path": "sequential.txt", "start_no": 3, "end_no": 3, "start_anchor": "line3", "end_anchor": "line3", "new_string": "NEW3\n"},
            ctx,
        )
        assert r2.metadata.get("error") is not True

        assert f.read_text() == "line1\nNEW2\nNEW3\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_new_string_with_leading_and_trailing_newline(self, tmp_path):
        """new_string starting with \\n and ending with \\n: intentional blank line before and after."""
        f = tmp_path / "lead-trail.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "lead-trail.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "lead-trail.txt", "start_no": 2, "end_no": 2, "start_anchor": "line2", "end_anchor": "line2", "new_string": "\nNEW2\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\n\nNEW2\nline3\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_replace_with_empty(self, tmp_path):
        """Delete a line (empty new_string): next line must not be corrupted."""
        f = tmp_path / "delete.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "delete.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "delete.txt", "start_no": 2, "end_no": 2, "start_anchor": "line2", "end_anchor": "line2", "new_string": ""},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nline3\n"

    @pytest.mark.asyncio
    async def test_replace_suffix_partial_match_replaces_whole_line(self, tmp_path):
        """A suffix substring in the middle of the line still replaces the whole line."""
        f = tmp_path / "partial-suffix.txt"
        f.write_text("    remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "partial-suffix.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "partial-suffix.txt",
                "start_no": 1,
                "end_no": 1,
                "start_anchor": "remap_read_coverage_from_file_diff",
                "end_anchor": "remap_read_coverage_from_file_diff",
                "new_string": "replacement()",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "replacement()\n"

    @pytest.mark.asyncio
    async def test_replace_suffix_at_line_end_replaces_whole_line(self, tmp_path):
        f = tmp_path / "full-suffix.txt"
        f.write_text("    remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "full-suffix.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "full-suffix.txt",
                "start_no": 1,
                "end_no": 1,
                "start_anchor": "remap_read_coverage_from_file_diff",
                "end_anchor": "old_ranges=old_ranges)",
                "new_string": "replacement()\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "replacement()\n"

    @pytest.mark.asyncio
    async def test_replace_suffix_partial_midline_does_not_leave_tail(self, tmp_path):
        f = tmp_path / "midline-suffix.py"
        f.write_text('    if new_string.endswith("\\n") and tail.startswith("\\n"):\n    content = "hello"\n')
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "midline-suffix.py"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "midline-suffix.py",
                "start_no": 1,
                "end_no": 1,
                "start_anchor": "if new_string.endswith",
                "end_anchor": "tail.startswith",
                "new_string": '    if (new_string == "" or new_string.endswith("\\n")) and tail.startswith("\\n"):',
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        content = f.read_text()
        assert content == '    if (new_string == "" or new_string.endswith("\\n")) and tail.startswith("\\n"):\n    content = "hello"\n'

    @pytest.mark.asyncio
    async def test_replace_single_line_prefix_eq_suffix_avoids_cross_line(self, tmp_path):
        """Single-line replace: prefix and suffix on different lines must not silently expand range."""
        f = tmp_path / "crossline.txt"
        f.write_text("    return\n    offset = 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "crossline.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "crossline.txt",
                "start_no": 1,
                "end_no": 1,
                "start_anchor": "return",
                "end_anchor": "offset",
                "new_string": "REPLACED",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert f.read_text() == "    return\n    offset = 1\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_different_prefix_suffix_on_same_line(self, tmp_path):
        """Single-line replace: different prefix/suffix both on the target line should work."""
        f = tmp_path / "same-line.txt"
        f.write_text("    return offset + 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "same-line.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "same-line.txt",
                "start_no": 1,
                "end_no": 1,
                "start_anchor": "return",
                "end_anchor": "offset",
                "new_string": "    return value + 1",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "    return value + 1\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_prefix_eq_suffix_duplicate_lines(self, tmp_path):
        """Single-line replace: prefix==suffix with duplicate lines, start_no disambiguates."""
        f = tmp_path / "dup.txt"
        f.write_text("    pass\n    pass\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "dup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "dup.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "pass",
                "end_anchor": "pass",
                "new_string": "DONE",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 2
        assert f.read_text() == "    pass\nDONE\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_equidistant_ambiguity_still_errors(self, tmp_path):
        """Single-line replace: equidistant duplicate lines should still report ambiguity."""
        f = tmp_path / "equidistant.txt"
        f.write_text("    pass\n    x = 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "equidistant.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "equidistant.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "pass",
                "end_anchor": "pass",
                "new_string": "DONE",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "ambiguous" in result.output.lower()
        assert f.read_text() == "    pass\n    x = 1\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_suffix_not_on_prefix_line_errors(self, tmp_path):
        """Single-line replace: suffix found nearby but not on the same line as prefix should error."""
        f = tmp_path / "suffix-wrong-line.txt"
        f.write_text("    return\n    offset = 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "suffix-wrong-line.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "suffix-wrong-line.txt",
                "start_no": 1,
                "end_no": 1,
                "start_anchor": "return",
                "end_anchor": "offset",
                "new_string": "REPLACED",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert f.read_text() == "    return\n    offset = 1\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_empty_prefix_suffix_on_empty_line(self, tmp_path):
        """Single-line replace: empty prefix/suffix matching an empty line."""
        f = tmp_path / "empty-line.txt"
        f.write_text("top\n\nbody\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "empty-line.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-line.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "",
                "end_anchor": "",
                "new_string": "INSERTED",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 2
        assert f.read_text() == "top\nINSERTED\nbody\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_cross_line_rejection_message(self, tmp_path):
        """Single-line replace: cross-line rejection mentions suffix not on same line."""
        f = tmp_path / "crossline-msg.txt"
        f.write_text("    return\n    offset = 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "crossline-msg.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "crossline-msg.txt",
                "start_no": 1,
                "end_no": 1,
                "start_anchor": "return",
                "end_anchor": "offset",
                "new_string": "REPLACED",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "not on the same line" in result.output

    @pytest.mark.asyncio
    async def test_replace_span_tolerance_scales_with_range_size(self, tmp_path):
        """Span tolerance scales: max(2, expected_span // 10)."""
        f = tmp_path / "tolerance.txt"
        lines = [f"line {i}" for i in range(1, 31)]
        f.write_text("\n".join(lines) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "tolerance.txt"}, ctx)

        # Replace lines 1-20 (span=19), but actual range is 1-21 (drift=1).
        # With old fixed tolerance=2 this would pass anyway, but with scaling
        # tolerance = max(2, 19//10) = max(2, 1) = 2, still passes.
        # The real test: replace lines 1-30 (span=29), actual 1-32 (drift=2).
        # tolerance = max(2, 29//10) = max(2, 2) = 2, drift=2 < 2 is false.
        # So we test that a drift of 2 is accepted when span >= 20.
        # Build a file where prefix is on line 1, suffix on line 22 (drift=2 from declared 1-20).
        f2 = tmp_path / "tolerance2.txt"
        f2.write_text("\n".join(f"line {i}" for i in range(1, 41)) + "\n")
        await r.execute_tool("read", {"file_path": "tolerance2.txt"}, ctx)

        # Declared range 1-20, but suffix "line 22" is on line 22 (drift=2).
        # tolerance = max(2, 19//10) = 2, drift=2 >= 2 → rejected.
        result = await r.execute_tool(
            "replace",
            {
                "file_path": "tolerance2.txt",
                "start_no": 1,
                "end_no": 20,
                "start_anchor": "line 1",
                "end_anchor": "line 22",
                "new_string": "REPLACED\n",
            },
            ctx,
        )
        assert result.metadata.get("error")

    @pytest.mark.asyncio
    async def test_replace_error_includes_window_snippet(self, tmp_path):
        """Error message includes lines around the target for context."""
        f = tmp_path / "snippet.txt"
        f.write_text("alpha\nbeta\ngamma\ndelta\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "snippet.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "snippet.txt", "start_no": 2, "end_no": 2, "start_anchor": "nonexistent", "end_anchor": "nonexistent", "new_string": "X"},
            ctx,
        )

        assert result.metadata.get("error")
        assert "2:" in result.output
        assert "beta" in result.output

    @pytest.mark.asyncio
    async def test_replace_error_suffix_mismatch_includes_window_snippet(self, tmp_path):
        """Suffix-not-on-same-line error includes window snippet."""
        f = tmp_path / "suffix-snippet.txt"
        f.write_text("    return\n    offset = 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "suffix-snippet.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "suffix-snippet.txt",
                "start_no": 1,
                "end_no": 1,
                "start_anchor": "return",
                "end_anchor": "offset",
                "new_string": "REPLACED",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "1:" in result.output
        assert "return" in result.output

    @pytest.mark.asyncio
    async def test_replace_tail_dedup_consecutive_duplicate_line(self, tmp_path):
        """If the last line of new_string matches the next line, the next line is consumed."""
        f = tmp_path / "dedup.txt"
        f.write_text("header\nimport os\nimport os\nfooter\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "dedup.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "import os",
                "end_anchor": "import os",
                "new_string": "import os\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["end_line"] == 3
        assert f.read_text() == "header\nimport os\nfooter\n"

    @pytest.mark.asyncio
    async def test_replace_tail_dedup_no_match_leaves_next_line(self, tmp_path):
        """If the last line of new_string does NOT match the next line, next line is preserved."""
        f = tmp_path / "no-dedup.txt"
        f.write_text("header\nimport os\nimport sys\nfooter\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "no-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "no-dedup.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "import os",
                "end_anchor": "import os",
                "new_string": "import os\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["end_line"] == 2
        assert f.read_text() == "header\nimport os\nimport sys\nfooter\n"

    @pytest.mark.asyncio
    async def test_replace_tail_dedup_multiline_new_string(self, tmp_path):
        """Tail dedup works with multi-line new_string where only the last line matters."""
        f = tmp_path / "multi-dedup.txt"
        f.write_text("start\nold_line\nold_line\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "multi-dedup.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "old_line",
                "end_anchor": "old_line",
                "new_string": "new_A\nold_line\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["end_line"] == 3
        assert f.read_text() == "start\nnew_A\nold_line\nend\n"

    @pytest.mark.asyncio
    async def test_replace_tail_dedup_at_file_end(self, tmp_path):
        """Tail dedup at end of file: last line matches, no next line to consume."""
        f = tmp_path / "end-dedup.txt"
        f.write_text("start\nold_line\nold_line\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "end-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "end-dedup.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "old_line",
                "end_anchor": "old_line",
                "new_string": "old_line\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["end_line"] == 3
        assert f.read_text() == "start\nold_line\n"

    @pytest.mark.asyncio
    async def test_replace_tail_dedup_empty_line_not_consumed(self, tmp_path):
        """Empty line dedup is skipped — only non-empty duplicates are consumed."""
        f = tmp_path / "empty-dedup.txt"
        f.write_text("start\n\n\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "empty-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-dedup.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "",
                "end_anchor": "",
                "new_string": "\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "start\n\n\nend\n"

    @pytest.mark.asyncio
    async def test_replace_tail_dedup_no_trailing_newline_in_new_string(self, tmp_path):
        """Dedup works when new_string does NOT end with \\n but last line matches tail."""
        f = tmp_path / "no-nl-dedup.txt"
        f.write_text("header\nimport os\nimport os\nfooter\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "no-nl-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "no-nl-dedup.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "import os",
                "end_anchor": "import os",
                "new_string": "import os",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["end_line"] == 3
        assert f.read_text() == "header\nimport os\nfooter\n"

    # ── Head-line dedup ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_replace_head_dedup_consecutive_duplicate_line(self, tmp_path):
        """If the first line of new_string matches the line before the replaced range, that line is consumed."""
        f = tmp_path / "head-dedup.txt"
        f.write_text("header\nimport os\nimport os\nfooter\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "head-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "head-dedup.txt",
                "start_no": 3,
                "end_no": 3,
                "start_anchor": "import os",
                "end_anchor": "import os",
                "new_string": "import os\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert f.read_text() == "header\nimport os\nfooter\n"
        # diff reflects the consumed duplicate line as a deletion
        assert "@@ -1,4 +1,3 @@" in result.diff
        assert "-import os" in result.diff

    @pytest.mark.asyncio
    async def test_replace_head_dedup_no_match_leaves_prev_line(self, tmp_path):
        """If the first line of new_string does NOT match the previous line, prev line is preserved."""
        f = tmp_path / "head-no-dedup.txt"
        f.write_text("header\nimport sys\nimport os\nfooter\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "head-no-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "head-no-dedup.txt",
                "start_no": 3,
                "end_no": 3,
                "start_anchor": "import os",
                "end_anchor": "import os",
                "new_string": "import os\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 3
        assert f.read_text() == "header\nimport sys\nimport os\nfooter\n"

    @pytest.mark.asyncio
    async def test_replace_head_dedup_multiline_new_string(self, tmp_path):
        """Head dedup works with multi-line new_string where only the first line matters."""
        f = tmp_path / "head-multi-dedup.txt"
        f.write_text("start\nold_line\nold_line\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "head-multi-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "head-multi-dedup.txt",
                "start_no": 3,
                "end_no": 3,
                "start_anchor": "old_line",
                "end_anchor": "old_line",
                "new_string": "old_line\nnew_B\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert f.read_text() == "start\nold_line\nnew_B\nend\n"

    @pytest.mark.asyncio
    async def test_replace_head_dedup_at_file_start(self, tmp_path):
        """Head dedup at start of file: no previous line to consume, nothing changes."""
        f = tmp_path / "head-start-dedup.txt"
        f.write_text("old_line\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "head-start-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "head-start-dedup.txt",
                "start_no": 1,
                "end_no": 1,
                "start_anchor": "old_line",
                "end_anchor": "old_line",
                "new_string": "old_line\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 1
        assert f.read_text() == "old_line\nend\n"

    @pytest.mark.asyncio
    async def test_replace_head_dedup_empty_line_not_consumed(self, tmp_path):
        """Empty line head dedup is skipped — only non-empty duplicates are consumed."""
        f = tmp_path / "head-empty-dedup.txt"
        f.write_text("start\n\n\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "head-empty-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "head-empty-dedup.txt",
                "start_no": 3,
                "end_no": 3,
                "start_anchor": "",
                "end_anchor": "",
                "new_string": "\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "start\n\n\nend\n"

    @pytest.mark.asyncio
    async def test_replace_head_and_tail_dedup_both_trigger(self, tmp_path):
        """When new_string first line matches prev and last line matches next, both are consumed."""
        f = tmp_path / "both-dedup.txt"
        f.write_text("dup_a\nold\ndup_b\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "both-dedup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "both-dedup.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "old",
                "end_anchor": "old",
                "new_string": "dup_a\nnew\ndup_b\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 1
        assert result.metadata["end_line"] == 3
        assert f.read_text() == "dup_a\nnew\ndup_b\n"
        # diff shows old replaced by new, with dup_a/dup_b as context lines
        assert "@@ -1,3 +1,3 @@" in result.diff
        assert "-old" in result.diff
        assert "+new" in result.diff

class TestDriftFallback:
    def _make_lines(self):
        # 10 行,edit 后 l2-l6 被替换成 X,文件变成 6 行
        return ["l1", "X", "l7", "l8", "l9", "l10"]

    def _make_map(self, epoch=1):
        from voidx.tools.file_state import LineDriftMap, ReadLineRange, DiffSpan
        # read epoch 记录的是原始 1-10;edit 20-30 -> 5行 的等价:这里用 2-6 -> 1行 (偏移 -4)
        return LineDriftMap(
            epoch=epoch,
            source_ranges=[ReadLineRange(1, 10)],
            span_steps=[[DiffSpan(2, 6, -4)]],
        )

    def test_first_match_succeeds_no_fallback(self):
        from voidx.tools.file_ops.edit_execute import _find_text_segment_with_drift_fallback

        lines = self._make_lines()
        # 用当前文件行号直接匹配成功
        result = _find_text_segment_with_drift_fallback(
            lines, 2, 2, "X", "X", [self._make_map()]
        )
        assert result.match is not None
        assert result.matched_map is None
        assert result.remapped_range is None

    def test_fallback_remaps_and_matches(self):
        from voidx.tools.file_ops.edit_execute import _find_text_segment_with_drift_fallback

        lines = self._make_lines()
        # LLM 用老行号 7-7 (实际在当前文件第 3 行),anchor "l7"
        # 首次在 ±3 搜索 7-7:lines[6..9] 不存在或不是 l7 -> 失败
        # 回退:remap 7 -> 3,重试匹配 l7 -> 成功
        result = _find_text_segment_with_drift_fallback(
            lines, 7, 7, "l7", "l7", [self._make_map()]
        )
        assert result.match is not None
        assert result.matched_map is not None
        assert result.remapped_range == (3, 3)

    def test_fallback_remap_to_wrong_content_fails(self):
        from voidx.tools.file_ops.edit_execute import _find_text_segment_with_drift_fallback
        from voidx.tools.file_state import LineDriftMap, ReadLineRange, DiffSpan

        # 文件 10 行,edit 把 2-6 删成 1 行,LLM 用老行号 9 找 "target"
        # remap 9 -> 4,但第 4 行是 "l8" 不是 "target",±3 内也没有
        lines = ["l1", "X", "l7", "l8", "l9", "l10"]
        bad_map = LineDriftMap(
            epoch=1,
            source_ranges=[ReadLineRange(1, 10)],
            span_steps=[[DiffSpan(2, 6, -5)]],
        )
        result = _find_text_segment_with_drift_fallback(
            lines, 9, 9, "target", "target", [bad_map]
        )
        assert result.match is None
        assert result.error is not None

    def test_multiple_candidates_same_range_equivalent(self):
        from voidx.tools.file_ops.edit_execute import _find_text_segment_with_drift_fallback

        lines = self._make_lines()
        # 两个 map 都 remap 到 (3,3),都匹配 l7 -> 等价命中
        maps = [self._make_map(epoch=1), self._make_map(epoch=2)]
        result = _find_text_segment_with_drift_fallback(
            lines, 7, 7, "l7", "l7", maps
        )
        assert result.match is not None

    def test_multiple_candidates_different_range_ambiguity(self):
        from voidx.tools.file_ops.edit_execute import _find_text_segment_with_drift_fallback
        from voidx.tools.file_state import LineDriftMap, ReadLineRange, DiffSpan

        # 20 行文件,第 9 行和第 17 行都是 "dup",相隔 8 行 (> 2*radius)
        lines = [f"l{i}" for i in range(1, 21)]
        lines[8] = "dup"   # 第 9 行
        lines[16] = "dup"  # 第 17 行
        # LLM 用老行号 5,首次在 5±3 (2-8) 搜索 -> 无 dup -> 失败
        # map1: DiffSpan(1,1,0) 无偏移,remap 5 -> 5,但 5±3 (2-8) 无 dup -> 跳过
        # 改用:map1 remap 5 -> 9 (offset +4),map2 remap 5 -> 17 (offset +12)
        # 但 5 必须在 span 之后。用 DiffSpan(1,2,-1): remap 5 -> 4? 不行。
        # 直接用 span 不覆盖 5: DiffSpan(1,1,4) -> remap 5 -> 9
        # DiffSpan(1,1,12) -> remap 5 -> 17
        map1 = LineDriftMap(
            epoch=1, source_ranges=[ReadLineRange(1, 20)],
            span_steps=[[DiffSpan(1, 1, 4)]],
        )
        map2 = LineDriftMap(
            epoch=2, source_ranges=[ReadLineRange(1, 20)],
            span_steps=[[DiffSpan(1, 1, 12)]],
        )
        result = _find_text_segment_with_drift_fallback(
            lines, 5, 5, "dup", "dup", [map1, map2]
        )
        assert result.match is None
        assert "ambig" in result.error.lower()

    def test_no_maps_returns_first_error(self):
        from voidx.tools.file_ops.edit_execute import _find_text_segment_with_drift_fallback

        lines = self._make_lines()
        result = _find_text_segment_with_drift_fallback(
            lines, 7, 7, "l7", "l7", []
        )
        assert result.match is None
        assert result.error is not None


class TestDriftFallbackE2E:
    @pytest.mark.asyncio
    async def test_drift_fallback_e2e(self, tmp_path):
        f = tmp_path / "drift.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "drift.txt"}, ctx)

        # edit: l2-l6 (5行) -> X (1行),偏移 -4,l7 从第 7 行变成第 3 行
        await r.execute_tool(
            "replace",
            {"file_path": "drift.txt", "start_no": 2, "end_no": 6,
             "start_anchor": "l2", "end_anchor": "l6", "new_string": "X"},
            ctx,
        )

        # LLM 用老行号 7-7 找 "l7",首次在 7±3 搜索失败,回退 remap 7->3 匹配成功
        result = await r.execute_tool(
            "replace",
            {"file_path": "drift.txt", "start_no": 7, "end_no": 7,
             "start_anchor": "l7", "end_anchor": "l7", "new_string": "L7"},
            ctx,
        )
        assert result.metadata.get("error") is not True
        assert "drift fallback" in result.output.lower()
        assert f.read_text() == "l1\nX\nL7\nl8\nl9\nl10\n"

    @pytest.mark.asyncio
    async def test_drift_fallback_accumulates_multiple_edits(self, tmp_path):
        """两次 edit 后用最初 read 的老行号走 fallback,验证 step 序列累积正确。

        read 1-10
        edit1: l2-l4 -> X (3行->1行,偏移 -2),l10 从第 10 行 -> 第 8 行
        edit2: l5-l7 -> Y (3行->1行,偏移 -2),l10 从第 8 行 -> 第 6 行
        LLM 用老行号 10-10 找 "l10":首次 10±3=7-13 搜索,第 6 行不在范围 -> 失败
        回退: remap 10 -> 8 (edit1) -> 6 (edit2),重试匹配 l10 -> 成功
        """
        f = tmp_path / "drift_multi.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "drift_multi.txt"}, ctx)

        # edit1: l2-l4 -> X (偏移 -2)
        await r.execute_tool(
            "replace",
            {"file_path": "drift_multi.txt", "start_no": 2, "end_no": 4,
             "start_anchor": "l2", "end_anchor": "l4", "new_string": "X"},
            ctx,
        )
        # edit2: l5-l7 -> Y (edit1 后 l5/l6/l7 仍在第 5-7 行,偏移 -2)
        await r.execute_tool(
            "replace",
            {"file_path": "drift_multi.txt", "start_no": 5, "end_no": 7,
             "start_anchor": "l5", "end_anchor": "l7", "new_string": "Y"},
            ctx,
        )

        # 当前文件: l1\nX\nY\nl8\nl9\nl10  -> l10 在第 6 行
        # LLM 用老行号 10-10 找 "l10",首次 10±3=7-13 搜索失败(文件只有 6 行)
        # 回退: remap 10 -> 8 (edit1) -> 6 (edit2),重试匹配 l10 -> 成功
        result = await r.execute_tool(
            "replace",
            {"file_path": "drift_multi.txt", "start_no": 10, "end_no": 10,
             "start_anchor": "l10", "end_anchor": "l10", "new_string": "L10"},
            ctx,
        )
        assert result.metadata.get("error") is not True
        assert "drift fallback" in result.output.lower()
        assert "epoch #1" in result.output
        assert f.read_text() == "l1\nX\nY\nl8\nl9\nL10\n"

    @pytest.mark.asyncio
    async def test_replace_anchor_leading_newline(self, tmp_path):
        """start_anchor with leading \\n should be normalized to its first non-empty line."""
        f = tmp_path / "lead-nl.txt"
        f.write_text("line1\ndef foo():\n    return 1\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "lead-nl.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "lead-nl.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "\ndef foo():",
                "end_anchor": "\ndef foo():",
                "new_string": "def bar():",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\ndef bar():\n    return 1\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_anchor_trailing_newline(self, tmp_path):
        """start_anchor with trailing \\n should be normalized to its first non-empty line."""
        f = tmp_path / "trail-nl.txt"
        f.write_text("line1\ndef foo():\n    return 1\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "trail-nl.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "trail-nl.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "def foo():\n",
                "end_anchor": "def foo():\n",
                "new_string": "def bar():",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\ndef bar():\n    return 1\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_anchor_middle_newline(self, tmp_path):
        """start_anchor with \\n in the middle should be normalized to its first non-empty line."""
        f = tmp_path / "mid-nl.txt"
        f.write_text("line1\ndef foo():\n    return 1\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "mid-nl.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "mid-nl.txt",
                "start_no": 2,
                "end_no": 3,
                "start_anchor": "def foo():\n    return 1",
                "end_anchor": "    return 1\ndef foo():",
                "new_string": "def bar():\n    return 2",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\ndef bar():\n    return 2\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_anchor_pure_newline_matches_empty_line(self, tmp_path):
        """anchor of pure \\n should be normalized to empty string and match an empty line."""
        f = tmp_path / "pure-nl.txt"
        f.write_text("line1\n\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "pure-nl.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "pure-nl.txt",
                "start_no": 2,
                "end_no": 2,
                "start_anchor": "\n",
                "end_anchor": "\n",
                "new_string": "INSERTED",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nINSERTED\nline3\n"
