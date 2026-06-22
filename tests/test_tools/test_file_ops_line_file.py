"""Tests for the file and line tools."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

import voidx.memory.store as store
from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry


def _history_rows(session_id: str = "sid-1") -> list[dict]:
    manifest = store.DATA_DIR / "sessions" / session_id / "file-history" / "manifest.jsonl"
    return [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
    ]


class TestFileTool:
    @pytest.mark.asyncio
    async def test_file_create_then_line_insert_writes_empty_file_without_read(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        created = await r.execute_tool("file", {"file_path": "new.txt", "op": "create"}, ctx)
        inserted = await r.execute_tool(
            "line",
            {"file_path": "new.txt", "op": "insert", "lineno": 0, "new_string": "hello\n"},
            ctx,
        )

        assert created.metadata.get("error") is not True
        assert inserted.metadata.get("error") is not True
        assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\n"

    @pytest.mark.asyncio
    async def test_file_create_overwrite_saves_version_and_clears_coverage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DATA_DIR", tmp_path / ".voidx")
        target = tmp_path / "existing.txt"
        target.write_text("old\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path), session_id="sid-1")
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "existing.txt"}, ctx)
        key = str(target.resolve())
        assert key in ctx.file_read_coverage

        result = await r.execute_tool(
            "file",
            {"file_path": "existing.txt", "op": "create", "overwrite": True},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == ""
        assert key not in ctx.file_read_coverage
        rows = _history_rows()
        assert rows[0]["path"] == "existing.txt"
        assert (store.DATA_DIR / "sessions" / "sid-1" / "file-history" / rows[0]["snapshot"]).read_text(encoding="utf-8") == "old\n"

    @pytest.mark.asyncio
    async def test_file_delete_saves_version_and_clears_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DATA_DIR", tmp_path / ".voidx")
        target = tmp_path / "delete.txt"
        target.write_text("gone\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path), session_id="sid-1")
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "delete.txt"}, ctx)
        key = str(target.resolve())

        result = await r.execute_tool("file", {"file_path": "delete.txt", "op": "delete"}, ctx)

        assert result.metadata.get("error") is not True
        assert not target.exists()
        assert key not in ctx.file_read_coverage
        assert key not in ctx.file_mtimes
        assert _history_rows()[0]["path"] == "delete.txt"

    @pytest.mark.asyncio
    async def test_file_move_migrates_coverage_and_mtime(self, tmp_path):
        source = tmp_path / "source.txt"
        dest = tmp_path / "nested" / "dest.txt"
        source.write_text("one\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "source.txt"}, ctx)
        source_key = str(source.resolve())

        result = await r.execute_tool(
            "file",
            {"file_path": "source.txt", "op": "move", "dest_path": "nested/dest.txt"},
            ctx,
        )

        dest_key = str(dest.resolve())
        assert result.metadata.get("error") is not True
        assert not source.exists()
        assert dest.read_text(encoding="utf-8") == "one\n"
        assert source_key not in ctx.file_read_coverage
        assert source_key not in ctx.file_mtimes
        assert dest_key in ctx.file_read_coverage
        assert dest_key in ctx.file_mtimes


class TestLineTool:
    @pytest.mark.asyncio
    async def test_line_insert_requires_read_coverage_for_non_empty_bof(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("one\ntwo\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        blocked = await r.execute_tool(
            "line",
            {"file_path": "target.txt", "op": "insert", "lineno": 0, "new_string": "zero\n"},
            ctx,
        )
        await r.execute_tool("read", {"file_path": "target.txt", "offset": 1, "limit": 1}, ctx)
        inserted = await r.execute_tool(
            "line",
            {"file_path": "target.txt", "op": "insert", "lineno": 0, "new_string": "zero\n"},
            ctx,
        )

        assert blocked.metadata.get("error") is True
        assert "read" in blocked.output.lower()
        assert inserted.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "zero\none\ntwo\n"

    @pytest.mark.asyncio
    async def test_line_insert_appends_at_end(self, tmp_path):
        target = tmp_path / "append.txt"
        target.write_text("one\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "append.txt"}, ctx)

        result = await r.execute_tool(
            "line",
            {"file_path": "append.txt", "op": "insert", "lineno": -1, "new_string": "two\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "one\ntwo\n"

    @pytest.mark.asyncio
    async def test_line_delete_range_requires_coverage_and_deletes_range(self, tmp_path):
        target = tmp_path / "delete-range.txt"
        target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        blocked = await r.execute_tool(
            "line",
            {"file_path": "delete-range.txt", "op": "delete", "lineno": 2, "end_no": 3},
            ctx,
        )
        await r.execute_tool("read", {"file_path": "delete-range.txt"}, ctx)

        result = await r.execute_tool(
            "line",
            {"file_path": "delete-range.txt", "op": "delete", "lineno": 2, "end_no": 3},
            ctx,
        )

        assert blocked.metadata.get("error") is True
        assert "read" in blocked.output.lower()
        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 3
        assert target.read_text(encoding="utf-8") == "one\nfour\n"
