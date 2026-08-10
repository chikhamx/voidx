"""Tests for file edit operations — replace and line insert via registry."""

from tests.tool_registry import build_registry
import sys
from pathlib import Path


import pytest

from voidx.tooling.application.execution import FileToolContext as ToolContext
from voidx.tooling.builtin.file.replace import FileReplaceTool
from voidx.tooling.builtin.file.replace_resolve import _find_text_segment
from voidx.tooling.application.registry import ToolRegistry
import voidx.tooling.application.file_state as file_state


class TestFileOpsCoverage:
    async def test_replace_preserves_read_coverage_after_success(self, tmp_path):
        f = tmp_path / "coverage.txt"
        f.write_text("one\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "coverage.txt"}, ctx)
        first = await r.execute_tool(
            "replace",
            {"file_path": "coverage.txt", "bounds": [{"line_no": 1, "anchor": "one"}], "new_string": "ONE"},
            ctx,
        )

        second = await r.execute_tool(
            "replace",
            {"file_path": "coverage.txt", "bounds": [{"line_no": 2, "anchor": "two"}], "new_string": "TWO"},
            ctx,
        )

        assert first.metadata.get("error") is not True
        assert second.metadata.get("error") is not True
        assert (tmp_path / "coverage.txt").read_text() == "ONE\nTWO\n"

    @pytest.mark.asyncio
    async def test_replace_unread_unique_anchor_succeeds(self, tmp_path):
        f = tmp_path / "retry-hint.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "retry-hint.txt", "offset": 1, "limit": 1}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "retry-hint.txt", "bounds": [{"line_no": 3, "anchor": "three"}], "new_string": "THREE"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "one\ntwo\nTHREE\n"

    @pytest.mark.asyncio
    async def test_replace_rejects_tracked_external_modification(self, tmp_path):
        f = tmp_path / "stale-replace.txt"
        f.write_text("one\ntwo\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "stale-replace.txt"}, ctx)
        f.write_text("external contents\n", encoding="utf-8")

        result = await r.execute_tool(
            "replace",
            {"file_path": "stale-replace.txt", "bounds": [{"line_no": 1, "anchor": "external"}], "new_string": "changed"},
            ctx,
        )

        assert result.metadata.get("error") is True
        assert "modified since last read" in result.output
        assert f.read_text(encoding="utf-8") == "external contents\n"

    @pytest.mark.asyncio
    async def test_replace_does_not_mark_unseen_lines_as_read_after_partial_edit(self, tmp_path):
        f = tmp_path / "partial-coverage.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 13)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "partial-coverage.txt", "offset": 1, "limit": 2}, ctx)

        edit = await r.execute_tool(
            "replace",
            {"file_path": "partial-coverage.txt", "bounds": [{"line_no": 2, "anchor": "line 2"}], "new_string": "LINE 2"},
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
        r = build_registry()
        await r.execute_tool("read", {"file_path": "expand-coverage.txt", "offset": 1, "limit": 30}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "expand-coverage.txt", "bounds": [{"line_no": 5, "anchor": "line 5"}], "new_string": "line 5a\nline 5b"},
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
        r = build_registry()
        await r.execute_tool("read", {"file_path": "delete-coverage.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "delete-coverage.txt", "bounds": [{"line_no": 50, "anchor": "line 50"}], "new_string": ""},
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
        r = build_registry()
        await r.execute_tool("read", {"file_path": "same-line.txt", "offset": 1, "limit": 2}, ctx)

        edit = await r.execute_tool(
            "replace",
            {"file_path": "same-line.txt", "bounds": [{"line_no": 2, "anchor": "two"}], "new_string": "TWO"},
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
        r = build_registry()
        await r.execute_tool("read", {"file_path": "multi-hunk-coverage.txt"}, ctx)

        r1 = await r.execute_tool(
            "replace",
            {"file_path": "multi-hunk-coverage.txt", "bounds": [{"line_no": 10, "anchor": "line 10"}], "new_string": "line 10a\nline 10b"},
            ctx,
        )
        r2 = await r.execute_tool(
            "replace",
            {"file_path": "multi-hunk-coverage.txt", "bounds": [{"line_no": 51, "anchor": "line 50"}], "new_string": ""},
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
        r = build_registry()
        await r.execute_tool("read", {"file_path": "noop-coverage.txt", "offset": 1, "limit": 1}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "noop-coverage.txt", "bounds": [{"line_no": 1, "anchor": "one"}], "new_string": "one"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert file_state.covered_read_range(ctx, f, 1, 1) is not None

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_replace_after_partial_edit_allows_unread_unique_target(self, tmp_path):
        f = tmp_path / "partial-edit.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 13)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "partial-edit.txt", "offset": 1, "limit": 2}, ctx)

        first = await r.execute_tool(
            "replace",
            {"file_path": "partial-edit.txt", "bounds": [{"line_no": 2, "anchor": "line 2"}], "new_string": "LINE 2"},
            ctx,
        )
        second = await r.execute_tool(
            "replace",
            {"file_path": "partial-edit.txt", "bounds": [{"line_no": 10, "anchor": "line 10"}], "new_string": "LINE 10"},
            ctx,
        )

        assert first.metadata.get("error") is not True
        assert second.metadata.get("error") is not True
        assert f.read_text().splitlines()[9] == "LINE 10"

    @pytest.mark.asyncio
    async def test_replace_spans_small_unread_gap_allowed(self, tmp_path):
        f = tmp_path / "gap-allowed.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "gap-allowed.txt", "offset": 1, "limit": 10}, ctx)
        await r.execute_tool("read", {"file_path": "gap-allowed.txt", "offset": 14, "limit": 7}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "gap-allowed.txt",
                "bounds": [{"line_no": 8, "anchor": "line 8"}, {"line_no": 16, "anchor": "line 16"}],
                "new_string": "replaced",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True

    @pytest.mark.asyncio
    async def test_replace_spans_large_unread_gap_succeeds(self, tmp_path):
        f = tmp_path / "gap-rejected.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "gap-rejected.txt", "offset": 1, "limit": 10}, ctx)
        await r.execute_tool("read", {"file_path": "gap-rejected.txt", "offset": 15, "limit": 6}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "gap-rejected.txt",
                "bounds": [{"line_no": 8, "anchor": "line 8"}, {"line_no": 16, "anchor": "line 16"}],
                "new_string": "replaced",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert "replaced" in f.read_text()
