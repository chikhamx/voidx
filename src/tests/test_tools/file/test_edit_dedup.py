"""Tests for file edit operations — replace and line insert via registry."""

import sys
from pathlib import Path


import pytest

from voidx.tools.base import ToolContext
from voidx.tools.file.replace import FileReplaceTool
from voidx.tools.file.replace_resolve import _find_text_segment
from voidx.tools.registry import ToolRegistry
import voidx.tools.file.replace as file_replace
import voidx.tools.file.state as file_state


class TestFileOpsDedup:
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
                "bounds": [{"line_no": 2, "anchor": "import os"}],
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
                "bounds": [{"line_no": 2, "anchor": "import os"}],
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
                "bounds": [{"line_no": 2, "anchor": "old_line"}],
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
                "bounds": [{"line_no": 2, "anchor": "old_line"}],
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
                "bounds": [{"line_no": 2, "anchor": ""}],
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
                "bounds": [{"line_no": 2, "anchor": "import os"}],
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
                "bounds": [{"line_no": 3, "anchor": "import os"}],
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
                "bounds": [{"line_no": 3, "anchor": "import os"}],
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
                "bounds": [{"line_no": 3, "anchor": "old_line"}],
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
                "bounds": [{"line_no": 1, "anchor": "old_line"}],
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
                "bounds": [{"line_no": 3, "anchor": ""}],
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
                "bounds": [{"line_no": 2, "anchor": "old"}],
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


class TestMultiLineDedup:
    """Tests for multi-line (up to 3) head/tail dedup."""

    @pytest.mark.asyncio
    async def test_head_dedup_two_lines(self, tmp_path):
        """Head dedup consumes 2 lines when new_string prefix matches 2 preceding lines."""
        f = tmp_path / "head2.txt"
        f.write_text("A\nB\nC\nold\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "head2.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "head2.txt",
                "bounds": [{"line_no": 4, "anchor": "old"}],
                "new_string": "B\nC\nnew\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert f.read_text() == "A\nB\nC\nnew\nend\n"

    @pytest.mark.asyncio
    async def test_head_dedup_three_lines(self, tmp_path):
        """Head dedup consumes 3 lines when new_string prefix matches 3 preceding lines."""
        f = tmp_path / "head3.txt"
        f.write_text("A\nB\nC\nD\nold\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "head3.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "head3.txt",
                "bounds": [{"line_no": 5, "anchor": "old"}],
                "new_string": "B\nC\nD\nnew\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert f.read_text() == "A\nB\nC\nD\nnew\nend\n"

    @pytest.mark.asyncio
    async def test_head_dedup_partial_match_stops_early(self, tmp_path):
        """Head dedup stops at first mismatch, consuming only matched lines."""
        f = tmp_path / "head-partial.txt"
        f.write_text("A\nX\nC\nold\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "head-partial.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "head-partial.txt",
                "bounds": [{"line_no": 4, "anchor": "old"}],
                "new_string": "X\nC\nnew\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        # C matches (line 3), X matches (line 2), A vs nothing — stop at 2
        assert result.metadata["start_line"] == 2
        assert f.read_text() == "A\nX\nC\nnew\nend\n"

    @pytest.mark.asyncio
    async def test_tail_dedup_two_lines(self, tmp_path):
        """Tail dedup consumes 2 lines when new_string suffix matches 2 following lines."""
        f = tmp_path / "tail2.txt"
        f.write_text("start\nold\nE\nF\nG\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "tail2.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "tail2.txt",
                "bounds": [{"line_no": 2, "anchor": "old"}],
                "new_string": "new\nE\nF\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["end_line"] == 4
        assert f.read_text() == "start\nnew\nE\nF\nG\n"

    @pytest.mark.asyncio
    async def test_tail_dedup_three_lines(self, tmp_path):
        """Tail dedup consumes 3 lines when new_string suffix matches 3 following lines."""
        f = tmp_path / "tail3.txt"
        f.write_text("start\nold\nE\nF\nG\nH\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "tail3.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "tail3.txt",
                "bounds": [{"line_no": 2, "anchor": "old"}],
                "new_string": "new\nE\nF\nG\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["end_line"] == 5
        assert f.read_text() == "start\nnew\nE\nF\nG\nH\n"

    @pytest.mark.asyncio
    async def test_tail_dedup_partial_match_stops_early(self, tmp_path):
        """Tail dedup stops at first mismatch, consuming only matched lines."""
        f = tmp_path / "tail-partial.txt"
        f.write_text("start\nold\nE\nX\nG\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "tail-partial.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "tail-partial.txt",
                "bounds": [{"line_no": 2, "anchor": "old"}],
                # last line "E" matches file line 3 (E), but "new2" != X — stop at 1
                "new_string": "new2\nE\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["end_line"] == 3
        assert f.read_text() == "start\nnew2\nE\nX\nG\n"

    @pytest.mark.asyncio
    async def test_head_and_tail_dedup_three_each(self, tmp_path):
        """Both head and tail dedup can consume up to 3 lines simultaneously."""
        f = tmp_path / "both3.txt"
        f.write_text("B\nC\nD\nold\nE\nF\nG\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "both3.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "both3.txt",
                "bounds": [{"line_no": 4, "anchor": "old"}],
                "new_string": "B\nC\nD\nnew\nE\nF\nG\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 1
        assert result.metadata["end_line"] == 7
        assert f.read_text() == "B\nC\nD\nnew\nE\nF\nG\n"

    @pytest.mark.asyncio
    async def test_dedup_capped_by_new_string_line_count(self, tmp_path):
        """Head + tail consumed lines must not exceed new_string total lines."""
        f = tmp_path / "cap.txt"
        # file: P\nQ\nold\nR\nS\n  — head could match P,Q (2 lines), tail could match R,S (2 lines)
        f.write_text("P\nQ\nold\nR\nS\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "cap.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "cap.txt",
                "bounds": [{"line_no": 3, "anchor": "old"}],
                # new_string has only 2 lines: Q (head) and R (tail)
                # head matches Q (1 line), tail matches R (1 line), total 2 = len(new_lines)
                "new_string": "Q\nR\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 4
        assert f.read_text() == "P\nQ\nR\nS\n"

    @pytest.mark.asyncio
    async def test_dedup_empty_line_not_consumed_multiline(self, tmp_path):
        """Empty lines are never consumed even in multi-line dedup."""
        f = tmp_path / "empty-multi.txt"
        f.write_text("start\n\n\n\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "empty-multi.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-multi.txt",
                "bounds": [{"line_no": 3, "anchor": ""}],
                "new_string": "\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "start\n\n\n\nend\n"

    @pytest.mark.asyncio
    async def test_head_dedup_at_file_boundary(self, tmp_path):
        """Head dedup stops when reaching file start, even if 3 lines could match."""
        f = tmp_path / "head-bound.txt"
        f.write_text("A\nB\nold\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "head-bound.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "head-bound.txt",
                "bounds": [{"line_no": 3, "anchor": "old"}],
                "new_string": "A\nB\nnew\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        # Only 2 lines available before start_line
        assert result.metadata["start_line"] == 1
        assert f.read_text() == "A\nB\nnew\nend\n"

    @pytest.mark.asyncio
    async def test_tail_dedup_at_file_boundary(self, tmp_path):
        """Tail dedup stops when reaching file end, even if 3 lines could match."""
        f = tmp_path / "tail-bound.txt"
        f.write_text("start\nold\nE\nF\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "tail-bound.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "tail-bound.txt",
                "bounds": [{"line_no": 2, "anchor": "old"}],
                "new_string": "new\nE\nF\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        # Only 2 lines available after end_line
        assert result.metadata["end_line"] == 4
        assert f.read_text() == "start\nnew\nE\nF\n"


    @pytest.mark.asyncio
    async def test_multiline_replace_with_head_and_tail_dedup(self, tmp_path):
        """Dedup works when the replaced range itself spans multiple lines."""
        f = tmp_path / "multi-range.txt"
        f.write_text("B\nC\nold1\nold2\nE\nF\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-range.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "multi-range.txt",
                "bounds": [
                    {"line_no": 3, "anchor": "old1"},
                    {"line_no": 4, "anchor": "old2"},
                ],
                "new_string": "B\nC\nnew\nE\nF\n",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 1
        assert result.metadata["end_line"] == 6
        assert f.read_text() == "B\nC\nnew\nE\nF\n"


class TestReplaceOverlapIntegration:
    @pytest.mark.asyncio
    async def test_replace_consumes_decorator_signature_overlap(self, tmp_path):
        f = tmp_path / "decorator.py"
        f.write_text("before\nslot\n@pytest.mark.asyncio\nasync def existing():\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "decorator.py", "offset": 2, "limit": 3}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "decorator.py",
                "bounds": [{"line_no": 2, "anchor": "slot"}],
                "new_string": "new_test = True\n\n@pytest.mark.asyncio\nasync def existing():",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["overlap"] == {"head": 0, "tail": 2}
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 4
        assert f.read_text() == "before\nnew_test = True\n\n@pytest.mark.asyncio\nasync def existing():\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_overlap_requires_coverage_for_consumed_tail(self, tmp_path):
        f = tmp_path / "coverage.py"
        original = "before\nold\ntail-1\ntail-2\nafter\n"
        f.write_text(original)
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "coverage.py", "offset": 2, "limit": 1}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "coverage.py",
                "bounds": [{"line_no": 2, "anchor": "old"}],
                "new_string": "new\ntail-1\ntail-2",
            },
            ctx,
        )

        assert result.metadata.get("error") is True
        assert "lines 2-4" in result.output
        assert f.read_text() == original

    @pytest.mark.asyncio
    async def test_replace_overlap_succeeds_with_effective_range_coverage(self, tmp_path):
        f = tmp_path / "covered.py"
        f.write_text("before\nold\ntail-1\ntail-2\nafter\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "covered.py", "offset": 2, "limit": 3}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "covered.py",
                "bounds": [{"line_no": 2, "anchor": "old"}],
                "new_string": "new\ntail-1\ntail-2",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["overlap"] == {"head": 0, "tail": 2}
        assert f.read_text() == "before\nnew\ntail-1\ntail-2\nafter\n"

    @pytest.mark.asyncio
    async def test_replace_identical_content_returns_no_changes_without_write(self, tmp_path, monkeypatch):
        f = tmp_path / "same.txt"
        f.write_text("head\nold\ntail\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "same.txt"}, ctx)
        writes: list[str] = []

        def record_write(_path, content):
            writes.append(content)
            return None

        monkeypatch.setattr(file_replace, "_safe_write_text", record_write)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "same.txt",
                "bounds": [{"line_no": 2, "anchor": "old"}],
                "new_string": "head\nold\ntail",
            },
            ctx,
        )

        assert result.title == "No changes"
        assert result.metadata["operations"] == 0
        assert result.metadata["overlap"] == {"head": 1, "tail": 1}
        assert "Boundary overlap" in result.output
        assert writes == []
