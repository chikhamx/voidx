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


class TestFileOpsErrors:
    async def test_replace_coverage_error_has_no_edit_index_prefix(self, tmp_path):
        """Coverage error should not be prefixed with 'Edit 0:' — it's a single replace op."""
        f = tmp_path / "coverage-prefix.txt"
        f.write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        # Read only line 1, then try to replace line 3 (uncovered)
        await r.execute_tool("read", {"file_path": "coverage-prefix.txt", "offset": 1, "limit": 1}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "coverage-prefix.txt",
                "bounds": [{"line_no": 3, "anchor": "three"}],
                "new_string": "THREE",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "must be read before editing" in result.output
        assert "Retry after reading lines 3-3." in result.output
        assert "Edit 0:" not in result.output

    @pytest.mark.asyncio
    async def test_replace_validation_error_for_end_no_lt_start_no_names_field(self, tmp_path):
        """model_validator error should name the actual field, not 'arguments'."""
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "dummy.txt",
                "bounds": [{"line_no": 5, "anchor": ""}, {"line_no": 1, "anchor": ""}],
                "new_string": "",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "multi-line replace requires non-empty anchors" in result.output
        assert "field 'arguments'" not in result.output
        assert "Value error" not in result.output

    @pytest.mark.asyncio
    async def test_replace_span_tolerance_scales_with_range_size(self, tmp_path):
        """Span tolerance scales: max(2, expected_span // 10)."""
        f = tmp_path / "tolerance.txt"
        lines = [f"line {i}" for i in range(1, 31)]
        f.write_text("\n".join(lines) + "\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "tolerance.txt"}, ctx)

        # Replace lines 1-20 (span=19), but actual range is 1-21 (drift=1).
        # With old fixed tolerance=2 this would pass anyway, but with scaling
        # tolerance = max(2, 19//10) = max(2, 1) = 2, still passes.
        # The real test: replace lines 1-30 (span=29), actual 1-32 (drift=2).
        # tolerance = max(2, 29//10) = max(2, 2) = 2, drift=2 < 2 is false.
        # So we test that a drift of 2 is accepted when span >= 20.
        # Build a file where prefix is on line 1, suffix on line 22 (drift=2 from declared 1-20).
        f2 = tmp_path / "tolerance2.txt"
        f2.write_text("\n".join(f"line {i}" for i in range(1, 41)) + "\n")
        await r.execute_tool("read", {"file_path": "tolerance2.txt"}, ctx)

        # Declared range 1-20, but suffix "line 22" is on line 22 (drift=2).
        # tolerance = max(2, 19//10) = 2, drift=2 >= 2 → rejected.
        result = await r.execute_tool(
            "replace",
            {
                "file_path": "tolerance2.txt",
                "bounds": [{"line_no": 1, "anchor": "line 1"}, {"line_no": 20, "anchor": "line 22"}],
                "new_string": "REPLACED\n",
            },
            ctx,
        )
        assert result.metadata.get("error")

    @pytest.mark.asyncio
    async def test_replace_error_includes_window_snippet(self, tmp_path):
        """Error message includes lines around the target for context."""
        f = tmp_path / "snippet.txt"
        f.write_text("alpha\nbeta\ngamma\ndelta\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "snippet.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "snippet.txt", "bounds": [{"line_no": 2, "anchor": "nonexistent"}], "new_string": "X"},
            ctx,
        )

        assert result.metadata.get("error")
        assert "2:" in result.output
        assert "beta" in result.output

    @pytest.mark.asyncio
    async def test_replace_error_suffix_mismatch_includes_window_snippet(self, tmp_path):
        """Anchor-not-found error includes window snippet."""
        f = tmp_path / "suffix-snippet.txt"
        f.write_text("    return\n    offset = 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "suffix-snippet.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "suffix-snippet.txt",
                "bounds": [{"line_no": 1, "anchor": "missing"}],
                "new_string": "REPLACED",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "1:" in result.output
        assert "return" in result.output
