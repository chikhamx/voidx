"""Tests for file edit operations — replace and line insert via registry."""

import sys
from pathlib import Path


import pytest

from voidx.tools.base import ToolContext
from voidx.tools.file_ops.edit_execute import FileReplaceTool
from voidx.tools.file_ops.edit_resolve import _find_text_segment
from voidx.tools.registry import ToolRegistry
import voidx.tools.file_state as file_state


class TestFileOpsReplace:
    async def test_sequential_replace_with_multiple_covered_ranges(self, tmp_path):
        f = tmp_path / "batch.txt"
        f.write_text("one\ntwo\nthree\nfour\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "batch.txt"}, ctx)

        r1 = await r.execute_tool(
            "replace",
            {"file_path": "batch.txt", "bounds": [{"line_no": 1, "anchor": "one"}], "new_string": "ONE"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "batch.txt", "bounds": [{"line_no": 3, "anchor": "three"}, {"line_no": 4, "anchor": "four"}], "new_string": "THREE\nFOUR\n"},
            ctx,
        )

        assert r1.metadata.get("error") is not True
        assert r2.metadata.get("error") is not True
        assert (tmp_path / "batch.txt").read_text() == "ONE\ntwo\nTHREE\nFOUR\n"

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
                "bounds": [{"line_no": 2, "anchor": "two"}],
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
                "bounds": [{"line_no": 6, "anchor": "target start"}, {"line_no": 8, "anchor": "target end"}],
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
            {"file_path": "overlap.txt", "bounds": [{"line_no": 1, "anchor": "one"}], "new_string": "x"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "overlap.txt", "bounds": [{"line_no": 2, "anchor": "two"}], "new_string": "y"},
            ctx,
        )

        assert r1.metadata.get("error") is not True
        assert r2.metadata.get("error") is not True
        assert (tmp_path / "overlap.txt").read_text() == "x\ny\nthree\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_deletes_text_segment_with_empty_new_string(self, tmp_path):
        f = tmp_path / "delete.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "delete.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "delete.txt", "bounds": [{"line_no": 2, "anchor": "two"}], "new_string": ""},
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
            {"file_path": "multi.txt", "bounds": [{"line_no": 1, "anchor": "def foo():"}, {"line_no": 2, "anchor": "pass"}], "new_string": "def foo():\n    return 42"},
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
            {"file_path": "window.txt", "bounds": [{"line_no": 1, "anchor": "line 40"}], "new_string": "LINE 40"},
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
            {"file_path": "nope.txt", "bounds": [{"line_no": 1, "anchor": "nonexistent"}], "new_string": "X"},
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
            {"file_path": "ambiguous.txt", "bounds": [{"line_no": 2, "anchor": "target"}], "new_string": "TARGET"},
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
                "bounds": [{"line_no": 1, "anchor": "hello"}, {"line_no": 3, "anchor": "world"}],
                "new_string": "replacement\n",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "No valid replace range found" in result.output
        assert f.read_text() == "hello world\nfoo bar\nbaz\n"

    @pytest.mark.asyncio
    async def test_replace_rejects_empty_prefix_for_empty_start_line(self, tmp_path):
        f = tmp_path / "empty-start.txt"
        f.write_text("top\n\nbody\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "empty-start.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-start.txt",
                "bounds": [{"line_no": 2, "anchor": ""}, {"line_no": 3, "anchor": "body"}],
                "new_string": "replacement\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is True
        assert "multi-line replace requires non-empty anchors" in result.output
        assert f.read_text() == "top\n\nbody\nend\n"

    @pytest.mark.asyncio
    async def test_replace_rejects_empty_suffix_for_empty_end_line(self, tmp_path):
        f = tmp_path / "empty-end.txt"
        f.write_text("top\nbody\n\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "empty-end.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-end.txt",
                "bounds": [{"line_no": 2, "anchor": "body"}, {"line_no": 3, "anchor": ""}],
                "new_string": "replacement\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is True
        assert "multi-line replace requires non-empty anchors" in result.output
        assert f.read_text() == "top\nbody\n\nend\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_both_anchors_empty_trusts_line_no(self, tmp_path):
        """Single-line replace (start_no==end_no) with both anchors empty should
        trust the line number instead of requiring an empty line."""
        f = tmp_path / "both-empty-single.txt"
        f.write_text("top\nbody\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "both-empty-single.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "both-empty-single.txt",
                "bounds": [{"line_no": 2, "anchor": ""}],
                "new_string": "replacement",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 2
        assert f.read_text() == "top\nreplacement\nend\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_both_anchors_empty_pure_insert(self, tmp_path):
        """Single-line replace with both anchors empty can be used as a pure
        insert: replace the target line with itself plus new content."""
        f = tmp_path / "pure-insert.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "pure-insert.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "pure-insert.txt",
                "bounds": [{"line_no": 2, "anchor": ""}],
                "new_string": "two\ninserted",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\ntwo\ninserted\nthree\n"

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
                "bounds": [{"line_no": 1, "anchor": "remap_read_coverage_from_file_diff"}],
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
                "bounds": [{"line_no": 1, "anchor": "remap_read_coverage_from_file_diff"}],
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
                "bounds": [{"line_no": 1, "anchor": "if new_string.endswith"}],
                "new_string": '    if (new_string == "" or new_string.endswith("\\n")) and tail.startswith("\\n"):',
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        content = f.read_text()
        assert content == '    if (new_string == "" or new_string.endswith("\\n")) and tail.startswith("\\n"):\n    content = "hello"\n'
