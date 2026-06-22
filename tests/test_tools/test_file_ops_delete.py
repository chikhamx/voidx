"""Tests for the line tool — delete operations."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry


class TestLineDelete:
    @pytest.mark.asyncio
    async def test_delete_single_line(self, tmp_path):
        f = tmp_path / "del.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "del.txt"}, ctx)

        result = await r.execute_tool(
            "line",
            {"file_path": "del.txt", "op": "delete", "lineno": 2},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\nthree\n"

    @pytest.mark.asyncio
    async def test_delete_first_line(self, tmp_path):
        f = tmp_path / "del-first.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "del-first.txt"}, ctx)

        result = await r.execute_tool(
            "line",
            {"file_path": "del-first.txt", "op": "delete", "lineno": 1},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "two\nthree\n"

    @pytest.mark.asyncio
    async def test_delete_last_line(self, tmp_path):
        f = tmp_path / "del-last.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "del-last.txt"}, ctx)

        result = await r.execute_tool(
            "line",
            {"file_path": "del-last.txt", "op": "delete", "lineno": 3},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\ntwo"

    @pytest.mark.asyncio
    async def test_delete_last_line_preserves_trailing_newline(self, tmp_path):
        f = tmp_path / "del-trailing.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "del-trailing.txt"}, ctx)

        result = await r.execute_tool(
            "line",
            {"file_path": "del-trailing.txt", "op": "delete", "lineno": 3},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\ntwo"

    @pytest.mark.asyncio
    async def test_delete_includes_start_end_line_metadata(self, tmp_path):
        f = tmp_path / "del-meta.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "del-meta.txt"}, ctx)

        result = await r.execute_tool(
            "line",
            {"file_path": "del-meta.txt", "op": "delete", "lineno": 2},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata.get("start_line") == 2
        assert result.metadata.get("end_line") == 2

    @pytest.mark.asyncio
    async def test_delete_requires_read_coverage(self, tmp_path):
        f = tmp_path / "del-unread.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "line",
            {"file_path": "del-unread.txt", "op": "delete", "lineno": 1},
            ctx,
        )

        assert "read" in result.output.lower()
        assert result.metadata.get("error") is True
        assert f.read_text() == "one\ntwo\n"

    @pytest.mark.asyncio
    async def test_delete_file_not_found(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "line",
            {"file_path": "nonexistent.txt", "op": "delete", "lineno": 1},
            ctx,
        )

        assert result.metadata.get("error") is True

    @pytest.mark.asyncio
    async def test_delete_produces_diff(self, tmp_path):
        f = tmp_path / "del-diff.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "del-diff.txt"}, ctx)

        result = await r.execute_tool(
            "line",
            {"file_path": "del-diff.txt", "op": "delete", "lineno": 2},
            ctx,
        )

        assert result.diff is not None
        assert "-two" in result.diff
