"""Tests for file edit operations — replace and line insert via registry."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.file_ops.edit_execute import FileReplaceTool
from voidx.tools.file_ops.edit_resolve import _find_text_segment
from voidx.tools.registry import ToolRegistry
import voidx.tools.file_state as file_state


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
