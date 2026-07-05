"""Tests for the file and write tools."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

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
            "write",
            {"file_path": "new.txt", "op": "insert", "lineno": 0, "new_string": "hello\n"},
            ctx,
        )

        assert created.metadata.get("error") is not True
        assert inserted.metadata.get("error") is not True
        assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\n"

    @pytest.mark.asyncio
    async def test_file_create_returns_next_step_hint_for_write_tool(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("file", {"file_path": "hint.txt", "op": "create"}, ctx)

        assert result.metadata.get("error") is not True
        assert "write tool" in result.next_step_hint
        assert "hint.txt" in result.next_step_hint
        assert 'op="append"' in result.next_step_hint

    @pytest.mark.asyncio
    async def test_file_create_overwrite_has_no_next_step_hint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DATA_DIR", tmp_path / ".voidx")
        (tmp_path / "existing.txt").write_text("old\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path), session_id="sid-1")
        r = ToolRegistry()

        result = await r.execute_tool(
            "file", {"file_path": "existing.txt", "op": "create", "overwrite": True}, ctx
        )

        assert result.metadata.get("error") is not True
        assert result.next_step_hint == ""

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


class TestWriteTool:
    @pytest.mark.asyncio
    async def test_line_insert_requires_read_coverage_for_non_empty_bof(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("one\ntwo\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        blocked = await r.execute_tool(
            "write",
            {"file_path": "target.txt", "op": "insert", "lineno": 0, "new_string": "zero\n"},
            ctx,
        )
        await r.execute_tool("read", {"file_path": "target.txt", "offset": 1, "limit": 1}, ctx)
        inserted = await r.execute_tool(
            "write",
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

        result = await r.execute_tool(
            "write",
            {"file_path": "append.txt", "op": "append", "new_string": "two\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "one\ntwo\n"


class TestWriteAppendOp:
    """Tests for op='append' — new feature from spec."""

    @pytest.mark.asyncio
    async def test_append_to_empty_file(self, tmp_path):
        target = tmp_path / "empty.txt"
        target.write_text("", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "empty.txt", "op": "append", "new_string": "first\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "first\n"

    @pytest.mark.asyncio
    async def test_append_to_non_empty_file(self, tmp_path):
        target = tmp_path / "has.txt"
        target.write_text("one\ntwo\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "has.txt", "op": "append", "new_string": "three\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"

    @pytest.mark.asyncio
    async def test_append_no_read_coverage_needed(self, tmp_path):
        target = tmp_path / "noverify.txt"
        target.write_text("existing\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        # No read call — append should still work

        result = await r.execute_tool(
            "write",
            {"file_path": "noverify.txt", "op": "append", "new_string": "added\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "existing\nadded\n"

    @pytest.mark.asyncio
    async def test_append_empty_new_string_returns_no_changes(self, tmp_path):
        target = tmp_path / "skip.txt"
        target.write_text("keep\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "skip.txt", "op": "append", "new_string": ""},
            ctx,
        )

        assert result.title == "No changes"
        assert target.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_append_with_lineno_ignored(self, tmp_path):
        target = tmp_path / "ignore.txt"
        target.write_text("data\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "ignore.txt", "op": "append", "lineno": 3, "new_string": "added\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "data\nadded\n"

    @pytest.mark.asyncio
    async def test_append_file_not_found(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "missing.txt", "op": "append", "new_string": "nope\n"},
            ctx,
        )

        assert result.metadata.get("error") is True


class TestWriteInsert0Based:
    """Tests for insert lineno 0-based (insert-before) semantics."""

    @pytest.mark.asyncio
    async def test_insert_lineno0_at_bof(self, tmp_path):
        target = tmp_path / "bof.txt"
        target.write_text("one\ntwo\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "bof.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "bof.txt", "op": "insert", "lineno": 0, "new_string": "zero\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "zero\none\ntwo\n"

    @pytest.mark.asyncio
    async def test_insert_lineno1_before_first_line(self, tmp_path):
        """lineno=1 means insert before line 1 (0-based), i.e. between line 1 and line 2 in 1-based."""
        target = tmp_path / "mid.txt"
        target.write_text("aaa\nbbb\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "mid.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "mid.txt", "op": "insert", "lineno": 1, "new_string": "inserted\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "aaa\ninserted\nbbb\n"

    @pytest.mark.asyncio
    async def test_insert_lineno_total_lines_no_coverage_needed(self, tmp_path):
        """lineno=total_lines is append position, no read coverage required."""
        target = tmp_path / "end.txt"
        target.write_text("one\ntwo\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        # No read call — inserting at total_lines should not require coverage

        result = await r.execute_tool(
            "write",
            {"file_path": "end.txt", "op": "insert", "lineno": 2, "new_string": "three\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"

    @pytest.mark.asyncio
    async def test_insert_lineno_beyond_total_lines_errors(self, tmp_path):
        target = tmp_path / "short.txt"
        target.write_text("only\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "short.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "short.txt", "op": "insert", "lineno": 5, "new_string": "oops\n"},
            ctx,
        )

        assert result.metadata.get("error") is True

    @pytest.mark.asyncio
    async def test_insert_lineno_negative_rejected(self, tmp_path):
        """lineno=-1 is no longer valid for insert; should be rejected."""
        target = tmp_path / "neg.txt"
        target.write_text("data\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "write",
            {"file_path": "neg.txt", "op": "insert", "lineno": -1, "new_string": "nope\n"},
            ctx,
        )

        assert result.metadata.get("error") is True