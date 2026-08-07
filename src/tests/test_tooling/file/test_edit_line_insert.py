"""Tests for file edit operations — replace and line insert via registry."""

from tests.tool_registry import build_registry
import sys
from pathlib import Path


import pytest

from voidx.tooling.application.execution import FileToolContext as ToolContext
from voidx.tooling.builtin.file.replace import FileReplaceTool
from voidx.tooling.builtin.file.replace_resolve import _find_text_segment
from voidx.tooling.application.registry import ToolRegistry
import voidx.tooling.builtin.file.replace as file_replace
import voidx.tooling.application.file_state as file_state


class TestFileOpsLineInsert:
    async def test_line_insert_uses_line_number_and_content_only(self, tmp_path):
        f = tmp_path / "insert-tool.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "insert-tool.txt", "offset": 1, "limit": 2}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "insert-tool.txt", "op": "insert", "lineno": 2, "new_string": "middle\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        # 1-based insert-before: lineno=2 means insert before line 2.
        assert (tmp_path / "insert-tool.txt").read_text() == "one\nmiddle\ntwo\n"

    @pytest.mark.asyncio
    async def test_line_insert_requires_read_coverage_for_target_line(self, tmp_path):
        f = tmp_path / "insert-unread.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()

        result = await r.execute_tool(
            "write",
            {"file_path": "insert-unread.txt", "op": "insert", "lineno": 2, "new_string": "middle\n"},
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
        r = build_registry()
        await r.execute_tool("read", {"file_path": "insert-bof.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "insert-bof.txt", "op": "insert", "lineno": 1, "new_string": "zero\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "zero\none\ntwo\n"

    @pytest.mark.asyncio
    async def test_merge_overlapping_read_ranges(self, tmp_path):
        f = tmp_path / "merge.txt"
        f.write_text("\n".join(str(i) for i in range(1, 101)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
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
        r = build_registry()
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
        r = build_registry()
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
        r = build_registry()

        result = await r.execute_tool(
            "write",
            {"file_path": "short.txt", "op": "insert", "lineno": 5, "new_string": "oops\n"},
            ctx,
        )

        assert result.metadata.get("error")
        assert "insert" in result.output.lower() or "line" in result.output.lower()

    @pytest.mark.asyncio
    async def test_line_insert_into_empty_file_at_lineno_1(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()

        result = await r.execute_tool(
            "write",
            {"file_path": "empty.txt", "op": "insert", "lineno": 1, "new_string": "first\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "first\n"

    @pytest.mark.asyncio
    async def test_line_insert_at_end_of_file(self, tmp_path):
        f = tmp_path / "end.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "end.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "end.txt", "op": "insert", "lineno": 3, "new_string": "three\n"},
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
        r = build_registry()

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
        r = build_registry()

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
        r = build_registry()
        await r.execute_tool("read", {"file_path": "remap.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "remap.txt", "op": "insert", "lineno": 3, "new_string": "inserted\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        reread = await r.execute_tool("read", {"file_path": "remap.txt", "offset": 11, "limit": 1}, ctx)
        assert reread.metadata.get("already_read")


class TestWriteInsertOverlap:
    @pytest.mark.asyncio
    async def test_insert_consumes_decorator_signature_tail_overlap(self, tmp_path):
        f = tmp_path / "decorator.py"
        f.write_text("before\n@pytest.mark.asyncio\nasync def existing():\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "decorator.py", "offset": 2, "limit": 2}, ctx)

        result = await r.execute_tool(
            "write",
            {
                "file_path": "decorator.py",
                "op": "insert",
                "lineno": 2,
                "new_string": "new_test = True\n\n@pytest.mark.asyncio\nasync def existing():",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["overlap"] == {"head": 0, "tail": 2}
        assert "Boundary overlap" in result.output
        assert f.read_text() == "before\nnew_test = True\n\n@pytest.mark.asyncio\nasync def existing():\n    pass\n"

    @pytest.mark.asyncio
    async def test_insert_consumes_head_overlap(self, tmp_path):
        f = tmp_path / "head.txt"
        f.write_text("keep\nhead-1\nhead-2\nafter\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "head.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {
                "file_path": "head.txt",
                "op": "insert",
                "lineno": 4,
                "new_string": "head-1\nhead-2\nnew",
            },
            ctx,
        )

        assert result.metadata["overlap"] == {"head": 2, "tail": 0}
        assert f.read_text() == "keep\nhead-1\nhead-2\nnew\nafter\n"

    @pytest.mark.asyncio
    async def test_insert_consumes_overlap_on_both_sides(self, tmp_path):
        f = tmp_path / "both.txt"
        f.write_text("keep\nhead\ntail\nafter\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "both.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {
                "file_path": "both.txt",
                "op": "insert",
                "lineno": 3,
                "new_string": "head\nnew\ntail",
            },
            ctx,
        )

        assert result.metadata["overlap"] == {"head": 1, "tail": 1}
        assert f.read_text() == "keep\nhead\nnew\ntail\nafter\n"

    @pytest.mark.asyncio
    async def test_insert_tail_overlap_requires_consumed_line_coverage(self, tmp_path):
        f = tmp_path / "tail-coverage.txt"
        original = "before\ntail-1\ntail-2\nafter\n"
        f.write_text(original)
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "tail-coverage.txt", "offset": 2, "limit": 1}, ctx)

        result = await r.execute_tool(
            "write",
            {
                "file_path": "tail-coverage.txt",
                "op": "insert",
                "lineno": 2,
                "new_string": "new\ntail-1\ntail-2",
            },
            ctx,
        )

        assert result.metadata.get("error") is True
        assert "lines 2-3" in result.output
        assert f.read_text() == original

    @pytest.mark.asyncio
    async def test_insert_head_overlap_requires_consumed_line_coverage(self, tmp_path):
        f = tmp_path / "head-coverage.txt"
        original = "head-1\nhead-2\ntarget\n"
        f.write_text(original)
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "head-coverage.txt", "offset": 3, "limit": 1}, ctx)

        result = await r.execute_tool(
            "write",
            {
                "file_path": "head-coverage.txt",
                "op": "insert",
                "lineno": 3,
                "new_string": "head-1\nhead-2\nnew",
            },
            ctx,
        )

        assert result.metadata.get("error") is True
        assert "lines 1-3" in result.output
        assert f.read_text() == original

    @pytest.mark.asyncio
    async def test_fully_overlapping_insert_returns_no_changes_without_write(self, tmp_path, monkeypatch):
        f = tmp_path / "same.txt"
        f.write_text("same\nafter\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "same.txt", "offset": 1, "limit": 1}, ctx)
        writes: list[str] = []

        def record_write(_path, content):
            writes.append(content)
            return None

        monkeypatch.setattr(file_replace, "_safe_write_text", record_write)

        result = await r.execute_tool(
            "write",
            {"file_path": "same.txt", "op": "insert", "lineno": 1, "new_string": "same"},
            ctx,
        )

        assert result.title == "No changes"
        assert result.metadata["operations"] == 0
        assert result.metadata["overlap"] == {"head": 0, "tail": 1}
        assert "Boundary overlap" in result.output
        assert writes == []

    @pytest.mark.asyncio
    async def test_insert_at_eof_with_overlap_omits_append_hint(self, tmp_path):
        f = tmp_path / "eof.txt"
        f.write_text("tail\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "eof.txt"}, ctx)

        result = await r.execute_tool(
            "write",
            {"file_path": "eof.txt", "op": "insert", "lineno": 2, "new_string": "tail"},
            ctx,
        )

        assert result.metadata["overlap"] == {"head": 1, "tail": 0}
        assert result.next_step_hint == ""
        assert f.read_text() == "tail\n"

    @pytest.mark.asyncio
    async def test_append_remains_literal_when_content_matches_file_tail(self, tmp_path):
        f = tmp_path / "append.txt"
        f.write_text("same\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()

        result = await r.execute_tool(
            "write",
            {"file_path": "append.txt", "op": "append", "new_string": "same\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert "overlap" not in result.metadata
        assert f.read_text() == "same\nsame\n"
