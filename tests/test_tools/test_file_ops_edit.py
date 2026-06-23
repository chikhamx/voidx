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
            {"file_path": "batch.txt", "start_no": 1, "end_no": 1, "prefix": "one", "suffix": "one", "new_string": "ONE"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "batch.txt", "start_no": 3, "end_no": 4, "prefix": "three", "suffix": "four", "new_string": "THREE\nFOUR\n"},
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
        await r.execute_tool("read", {"file_path": "insert-tool.txt", "offset": 1, "limit": 1}, ctx)

        result = await r.execute_tool(
            "line",
            {"file_path": "insert-tool.txt", "op": "insert", "lineno": 1, "new_string": "middle\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "insert-tool.txt").read_text() == "one\nmiddle\ntwo\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_insert_requires_read_coverage_for_target_line(self, tmp_path):
        f = tmp_path / "insert-unread.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "line",
            {"file_path": "insert-unread.txt", "op": "insert", "lineno": 1, "new_string": "middle\n"},
            ctx,
        )

        assert "read" in result.output.lower()
        assert result.metadata.get("error")
        assert f.read_text() == "one\ntwo\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_insert_at_beginning_of_file(self, tmp_path):
        f = tmp_path / "insert-bof.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "insert-bof.txt"}, ctx)

        result = await r.execute_tool(
            "line",
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
                "prefix": "two",
                "suffix": "two",
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
                "prefix": "target start",
                "suffix": "target end",
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
            {"file_path": "overlap.txt", "start_no": 1, "end_no": 1, "prefix": "one", "suffix": "one", "new_string": "x"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "overlap.txt", "start_no": 2, "end_no": 2, "prefix": "two", "suffix": "two", "new_string": "y"},
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
            {"file_path": "coverage.txt", "start_no": 1, "end_no": 1, "prefix": "one", "suffix": "one", "new_string": "ONE"},
            ctx,
        )

        second = await r.execute_tool(
            "replace",
            {"file_path": "coverage.txt", "start_no": 2, "end_no": 2, "prefix": "two", "suffix": "two", "new_string": "TWO"},
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
            {"file_path": "partial-coverage.txt", "start_no": 2, "end_no": 2, "prefix": "line 2", "suffix": "line 2", "new_string": "LINE 2"},
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
            {"file_path": "expand-coverage.txt", "start_no": 5, "end_no": 5, "prefix": "line 5", "suffix": "line 5", "new_string": "line 5a\nline 5b"},
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
            {"file_path": "delete-coverage.txt", "start_no": 50, "end_no": 50, "prefix": "line 50", "suffix": "line 50", "new_string": ""},
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
            {"file_path": "same-line.txt", "start_no": 2, "end_no": 2, "prefix": "two", "suffix": "two", "new_string": "TWO"},
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
            {"file_path": "multi-hunk-coverage.txt", "start_no": 10, "end_no": 10, "prefix": "line 10", "suffix": "line 10", "new_string": "line 10a\nline 10b"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "multi-hunk-coverage.txt", "start_no": 51, "end_no": 51, "prefix": "line 50", "suffix": "line 50", "new_string": ""},
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
            {"file_path": "noop-coverage.txt", "start_no": 1, "end_no": 1, "prefix": "one", "suffix": "one", "new_string": "one"},
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
            {"file_path": "partial-edit.txt", "start_no": 2, "end_no": 2, "prefix": "line 2", "suffix": "line 2", "new_string": "LINE 2"},
            ctx,
        )
        second = await r.execute_tool(
            "replace",
            {"file_path": "partial-edit.txt", "start_no": 10, "end_no": 10, "prefix": "line 10", "suffix": "line 10", "new_string": "LINE 10"},
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
            {"file_path": "merge.txt", "start_no": 1, "end_no": 1, "prefix": "1", "suffix": "1", "new_string": "CHANGED"},
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
            {"file_path": "adjacent.txt", "start_no": 50, "end_no": 51, "prefix": "50", "suffix": "51", "new_string": "MERGED"},
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
            {"file_path": "gap.txt", "start_no": 15, "end_no": 15, "prefix": "15", "suffix": "15", "new_string": "GAP"},
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
            "line",
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
            "line",
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
            "line",
            {"file_path": "end.txt", "op": "insert", "lineno": 2, "new_string": "three\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\ntwo\nthree\n"

    @pytest.mark.asyncio
    async def test_line_insert_at_end_of_file_with_lineno_minus_one(self, tmp_path):
        f = tmp_path / "end.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "end.txt"}, ctx)

        result = await r.execute_tool(
            "line",
            {"file_path": "end.txt", "op": "insert", "lineno": -1, "new_string": "three\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\ntwo\nthree\n"

    @pytest.mark.asyncio
    async def test_line_insert_into_empty_file_with_lineno_minus_one(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "line",
            {"file_path": "empty.txt", "op": "insert", "lineno": -1, "new_string": "first\n"},
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
            "line",
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
            {"file_path": "delete.txt", "start_no": 2, "end_no": 2, "prefix": "two", "suffix": "two", "new_string": ""},
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
            {"file_path": "multi.txt", "start_no": 1, "end_no": 2, "prefix": "def foo():", "suffix": "pass", "new_string": "def foo():\n    return 42"},
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
            {"file_path": "window.txt", "start_no": 1, "end_no": 1, "prefix": "line 40", "suffix": "line 40", "new_string": "LINE 40"},
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
            {"file_path": "nope.txt", "start_no": 1, "end_no": 1, "prefix": "nonexistent", "suffix": "nonexistent", "new_string": "X"},
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
            {"file_path": "ambiguous.txt", "start_no": 2, "end_no": 2, "prefix": "target", "suffix": "target", "new_string": "TARGET"},
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
                "prefix": "hello",
                "suffix": "world",
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
                "prefix": "",
                "suffix": "body",
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
                "prefix": "body",
                "suffix": "",
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
                "prefix": "",
                "suffix": "body",
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
            {"file_path": "trailing.txt", "start_no": 2, "end_no": 2, "prefix": "line2", "suffix": "line2", "new_string": "NEW2\n"},
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
            {"file_path": "multi-trailing.txt", "start_no": 2, "end_no": 3, "prefix": "line2", "suffix": "line3", "new_string": "NEW_A\nNEW_B\n"},
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
            {"file_path": "last-line.txt", "start_no": 3, "end_no": 3, "prefix": "line3", "suffix": "line3", "new_string": "NEW3\n"},
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
            {"file_path": "no-trailing.txt", "start_no": 2, "end_no": 2, "prefix": "line2", "suffix": "line2", "new_string": "NEW2\n"},
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
            {"file_path": "baseline.txt", "start_no": 2, "end_no": 2, "prefix": "line2", "suffix": "line2", "new_string": "NEW2"},
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
            {"file_path": "double-newline.txt", "start_no": 2, "end_no": 2, "prefix": "line2", "suffix": "line2", "new_string": "NEW2\n\n"},
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
            {"file_path": "code.py", "start_no": 1, "end_no": 2, "prefix": "def foo():", "suffix": "return 1", "new_string": "def foo():\n    return 42\n"},
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
            {"file_path": "multi-to-single.txt", "start_no": 2, "end_no": 4, "prefix": "line2", "suffix": "line4", "new_string": "REPLACED\n"},
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
            {"file_path": "single.txt", "start_no": 1, "end_no": 1, "prefix": "only_line", "suffix": "only_line", "new_string": "REPLACED\n"},
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
            {"file_path": "two.txt", "start_no": 1, "end_no": 1, "prefix": "line1", "suffix": "line1", "new_string": "NEW1\n"},
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
            {"file_path": "sequential.txt", "start_no": 2, "end_no": 2, "prefix": "line2", "suffix": "line2", "new_string": "NEW2\n"},
            ctx,
        )
        assert r1.metadata.get("error") is not True

        # Read again to update coverage after first edit
        await r.execute_tool("read", {"file_path": "sequential.txt"}, ctx)

        r2 = await r.execute_tool(
            "replace",
            {"file_path": "sequential.txt", "start_no": 3, "end_no": 3, "prefix": "line3", "suffix": "line3", "new_string": "NEW3\n"},
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
            {"file_path": "lead-trail.txt", "start_no": 2, "end_no": 2, "prefix": "line2", "suffix": "line2", "new_string": "\nNEW2\n"},
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
            {"file_path": "delete.txt", "start_no": 2, "end_no": 2, "prefix": "line2", "suffix": "line2", "new_string": ""},
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
                "prefix": "remap_read_coverage_from_file_diff",
                "suffix": "remap_read_coverage_from_file_diff",
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
                "prefix": "remap_read_coverage_from_file_diff",
                "suffix": "old_ranges=old_ranges)",
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
                "prefix": "if new_string.endswith",
                "suffix": "tail.startswith",
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
                "prefix": "return",
                "suffix": "offset",
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
                "prefix": "return",
                "suffix": "offset",
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
                "prefix": "pass",
                "suffix": "pass",
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
                "prefix": "pass",
                "suffix": "pass",
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
                "prefix": "return",
                "suffix": "offset",
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
                "prefix": "",
                "suffix": "",
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
                "prefix": "return",
                "suffix": "offset",
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
                "prefix": "line 1",
                "suffix": "line 22",
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
            {"file_path": "snippet.txt", "start_no": 2, "end_no": 2, "prefix": "nonexistent", "suffix": "nonexistent", "new_string": "X"},
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
                "prefix": "return",
                "suffix": "offset",
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
                "prefix": "import os",
                "suffix": "import os",
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
                "prefix": "import os",
                "suffix": "import os",
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
                "prefix": "old_line",
                "suffix": "old_line",
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
                "prefix": "old_line",
                "suffix": "old_line",
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
                "prefix": "",
                "suffix": "",
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
                "prefix": "import os",
                "suffix": "import os",
                "new_string": "import os",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["end_line"] == 3
        assert f.read_text() == "header\nimport os\nfooter\n"