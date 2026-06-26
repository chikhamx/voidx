"""Tests for file coverage fingerprint and replace/line tool integration."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry
import voidx.tools.file_state as file_state

class TestFileOps:
    @pytest.mark.asyncio
    async def test_read_coverage_uses_mtime_ns_and_size_fingerprint(self, tmp_path):
        import voidx.tools.file_state as fs

        f = tmp_path / "fp.txt"
        f.write_text("hello\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "fp.txt"}, ctx)

        key = str((tmp_path / "fp.txt").resolve())
        coverage = ctx.file_read_coverage[key]
        fp = fs.file_fingerprint(tmp_path / "fp.txt")
        assert coverage["fingerprint"] == {"mtime_ns": fp.mtime_ns, "size": fp.size}

        result = await r.execute_tool(
            "replace",
            {"file_path": "fp.txt", "start_no": 1, "end_no": 1, "prefix": "hello", "suffix": "hello", "new_string": "HELLO"},
            ctx,
        )
        assert result.metadata.get("error") is not True

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_insert_then_replace_handles_shifted_lines(self, tmp_path):
        f = tmp_path / "paragraph.py"
        f.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "def bar():\n"
            "    value = 2\n"
            "    return value\n"
        )
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph.py"}, ctx)

        shifted = await r.execute_tool(
            "write",
            {"file_path": "paragraph.py", "op": "insert", "lineno": 2, "new_string": "    extra = 0\n"},
            ctx,
        )
        corrected = await r.execute_tool(
            "replace",
            {"file_path": "paragraph.py", "start_no": 5, "end_no": 5, "prefix": "def bar():", "suffix": "def bar():", "new_string": "def baz():"},
            ctx,
        )

        assert shifted.metadata.get("error") is not True
        assert corrected.metadata.get("error") is not True
        assert "def baz():\n    value = 2" in (tmp_path / "paragraph.py").read_text()

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_reports_ambiguous_and_missing_matches(self, tmp_path):
        f = tmp_path / "paragraph-errors.py"
        f.write_text("target = 1\nother = 0\ntarget = 2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph-errors.py"}, ctx)

        ambiguous = await r.execute_tool(
            "replace",
            {"file_path": "paragraph-errors.py", "start_no": 2, "end_no": 2, "prefix": "target", "suffix": "target", "new_string": "changed"},
            ctx,
        )
        missing = await r.execute_tool(
            "replace",
            {"file_path": "paragraph-errors.py", "start_no": 2, "end_no": 2, "prefix": "missing", "suffix": "missing", "new_string": "changed"},
            ctx,
        )

        assert "ambiguous" in ambiguous.output.lower()
        assert ambiguous.metadata.get("error")
        assert "not found" in missing.output.lower()
        assert missing.metadata.get("error")
        assert (tmp_path / "paragraph-errors.py").read_text() == "target = 1\nother = 0\ntarget = 2\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_lineno_hint_disambiguates_nearest_prefix(self, tmp_path):
        f = tmp_path / "nearest.py"
        f.write_text("def item():\n    a = 1\n\ndef item():\n    a = 2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "nearest.py"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "nearest.py", "start_no": 4, "end_no": 5, "prefix": "def item():", "suffix": "a = 2", "new_string": "def item():\n    a = 3"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "nearest.py").read_text() == "def item():\n    a = 1\n\ndef item():\n    a = 3\n"

    @pytest.mark.asyncio
    async def test_replace_multiline_prefix_replaces_multiline_range(self, tmp_path):
        f = tmp_path / "multi-line-prefix.py"
        f.write_text("top\ninserted\nstart\nmiddle\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-line-prefix.py"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "multi-line-prefix.py", "start_no": 2, "end_no": 4, "prefix": "inserted", "suffix": "middle", "new_string": "START\nMIDDLE"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "multi-line-prefix.py").read_text() == "top\nSTART\nMIDDLE\nend\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_insert_after_shifted_line(self, tmp_path):
        f = tmp_path / "insert-paragraph-correct.py"
        f.write_text("top\ninserted\ntarget\nbottom\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "insert-paragraph-correct.py"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "insert-paragraph-correct.py", "op": "insert", "lineno": 3, "new_string": "after target\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert (tmp_path / "insert-paragraph-correct.py").read_text() == "top\ninserted\ntarget\nafter target\nbottom\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_still_requires_read_coverage(self, tmp_path):
        f = tmp_path / "paragraph-coverage.py"
        f.write_text("top\nmiddle\ntarget\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph-coverage.py", "offset": 1, "limit": 1}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "paragraph-coverage.py", "start_no": 1, "end_no": 1, "prefix": "target", "suffix": "target", "new_string": "TARGET"},
            ctx,
        )

        assert "read" in result.output.lower()
        assert result.metadata.get("error")
        assert (tmp_path / "paragraph-coverage.py").read_text() == "top\nmiddle\ntarget\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_sequential_replace_on_same_file(self, tmp_path):
        f = tmp_path / "paragraph-conflict.py"
        f.write_text("top\ntarget\nbottom\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "paragraph-conflict.py"}, ctx)

        r1 = await r.execute_tool(
            "replace",
            {"file_path": "paragraph-conflict.py", "start_no": 2, "end_no": 2, "prefix": "target", "suffix": "target", "new_string": "TARGET"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "paragraph-conflict.py", "start_no": 1, "end_no": 1, "prefix": "top", "suffix": "top", "new_string": "TOP"},
            ctx,
        )

        assert r1.metadata.get("error") is not True
        assert r2.metadata.get("error") is not True
        assert (tmp_path / "paragraph-conflict.py").read_text() == "TOP\nTARGET\nbottom\n"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_line_insert_and_replace_delete_report_line_shift(self, tmp_path):
        f = tmp_path / "shift.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "shift.txt"}, ctx)

        inserted = await r.execute_tool(
            "write",
            {"file_path": "shift.txt", "op": "insert", "lineno": 0, "new_string": "zero\n"},
            ctx,
        )
        deleted = await r.execute_tool(
            "replace",
            {"file_path": "shift.txt", "start_no": 3, "end_no": 3, "prefix": "two", "suffix": "two", "new_string": ""},
            ctx,
        )

        assert inserted.metadata.get("error") is not True
        assert "shift" in inserted.output.lower()
        assert deleted.metadata.get("error") is not True
        assert "shift" in deleted.output.lower()

    @pytest.mark.asyncio
    async def test_replace_same_line_count_does_not_report_shift(self, tmp_path):
        f = tmp_path / "same-lines.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "same-lines.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "same-lines.txt", "start_no": 2, "end_no": 2, "prefix": "two", "suffix": "two", "new_string": "TWO"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert "line shift" not in result.output.lower()

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_sequential_insert_and_delete_report_line_shifts(self, tmp_path):
        f = tmp_path / "multi-shift.txt"
        f.write_text("one\ntwo\nthree\nfour\nfive\nsix\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-shift.txt"}, ctx)

        inserted = await r.execute_tool(
            "write",
            {"file_path": "multi-shift.txt", "op": "insert", "lineno": 1, "new_string": "one-a\n"},
            ctx,
        )
        deleted = await r.execute_tool(
            "replace",
            {"file_path": "multi-shift.txt", "start_no": 6, "end_no": 6, "prefix": "five", "suffix": "five", "new_string": ""},
            ctx,
        )

        assert inserted.metadata.get("error") is not True
        assert "shift" in inserted.output.lower()
        assert deleted.metadata.get("error") is not True
        assert "shift" in deleted.output.lower()

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "nope.txt"}, ctx)
        assert "File not found" in result.output

    @pytest.mark.asyncio
    async def test_read_offset_beyond_file(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("line1\nline2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "short.txt", "offset": 100}, ctx)
        assert result.metadata["lines"] == 0
        assert "beyond" in result.output.lower() or "offset" in result.output.lower()
        assert result.metadata["error"] is True
        assert result.metadata["reason"] == "offset_beyond_eof"


