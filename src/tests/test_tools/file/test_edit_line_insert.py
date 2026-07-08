"""Tests for file edit operations — replace and line insert via registry."""

import sys
from pathlib import Path


import pytest

from voidx.tools.base import ToolContext
from voidx.tools.file.replace import FileReplaceTool
from voidx.tools.file.replace_resolve import _find_text_segment
from voidx.tools.registry import ToolRegistry
import voidx.tools.file.state as file_state


class TestFileOpsLineInsert:
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
    async def test_merge_overlapping_read_ranges(self, tmp_path):
        f = tmp_path / "merge.txt"
        f.write_text("\n".join(str(i) for i in range(1, 101)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "merge.txt", "offset": 1, "limit": 50}, ctx)
        await r.execute_tool("read", {"file_path": "merge.txt", "offset": 40, "limit": 61}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "merge.txt", "bounds": [{"line_no": 1, "anchor": "1"}], "new_string": "CHANGED"},
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
            {"file_path": "adjacent.txt", "bounds": [{"line_no": 50, "anchor": "50"}, {"line_no": 51, "anchor": "51"}], "new_string": "MERGED"},
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
            {"file_path": "gap.txt", "bounds": [{"line_no": 15, "anchor": "15"}], "new_string": "GAP"},
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
