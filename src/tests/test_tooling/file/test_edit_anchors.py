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


class TestFileOpsAnchors:
    async def test_replace_multi_line_empty_start_anchor_still_requires_empty_line(self, tmp_path):
        """Multi-line replace with empty start_anchor should still require an
        empty line — the relaxation only applies to single-line replace."""
        f = tmp_path / "empty-anchor-missing.txt"
        f.write_text("top\nbody\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "empty-anchor-missing.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-anchor-missing.txt",
                "bounds": [{"line_no": 2, "anchor": ""}, {"line_no": 3, "anchor": "end"}],
                "new_string": "replacement\n",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "multi-line replace requires non-empty anchors" in result.output
        assert f.read_text() == "top\nbody\nend\n"


    @pytest.mark.asyncio
    async def test_replace_single_line_empty_start_anchor_trusts_line_no(self, tmp_path):
        """Single-line replace (start_no==end_no) with empty start_anchor should
        trust the line number instead of requiring an empty line."""
        f = tmp_path / "empty-start-single.txt"
        f.write_text("top\nbody\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "empty-start-single.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-start-single.txt",
                "bounds": [{"line_no": 2, "anchor": ""}],
                "new_string": "replacement",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 2
        assert f.read_text() == "top\nreplacement\nend\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_empty_end_anchor_trusts_line_no(self, tmp_path):
        """Single-line replace (start_no==end_no) with empty end_anchor should
        trust the line number instead of requiring an empty line."""
        f = tmp_path / "empty-end-single.txt"
        f.write_text("top\nbody\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "empty-end-single.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-end-single.txt",
                "bounds": [{"line_no": 2, "anchor": "body"}],
                "new_string": "replacement",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 2
        assert f.read_text() == "top\nreplacement\nend\n"

    @pytest.mark.asyncio
    async def test_replace_multi_line_empty_anchor_still_errors(self, tmp_path):
        """Multi-line replace (start_no != end_no) with empty anchor should
        still require matching an empty line — the relaxation only applies
        to single-line replace."""
        f = tmp_path / "multi-empty.txt"
        f.write_text("alpha\nbeta\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "multi-empty.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "multi-empty.txt",
                "bounds": [{"line_no": 1, "anchor": ""}, {"line_no": 2, "anchor": "beta"}],
                "new_string": "x",
            },
            ctx,
        )

        assert result.metadata.get("error") is True
        assert "multi-line replace requires non-empty anchors" in result.output

    @pytest.mark.asyncio
    async def test_replace_single_line_empty_anchor_ignores_nearby_empty_line(self, tmp_path):
        """Regression: empty start_anchor must trust the target line number,
        not match a nearby empty line within the search radius."""
        f = tmp_path / "nearby-empty.txt"
        f.write_text("a\nb\n\nd\ntarget\nf\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "nearby-empty.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "nearby-empty.txt",
                "bounds": [{"line_no": 5, "anchor": ""}],
                "new_string": "REPLACED",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 5
        assert result.metadata["end_line"] == 5
        assert f.read_text() == "a\nb\n\nd\nREPLACED\nf\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_prefix_eq_suffix_avoids_cross_line(self, tmp_path):
        """Single-line replace: prefix and suffix on different lines must not silently expand range."""
        f = tmp_path / "crossline.txt"
        f.write_text("    return\n    offset = 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "crossline.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "crossline.txt",
                "bounds": [{"line_no": 1, "anchor": "return"}, {"line_no": 1, "anchor": "offset"}],
                "new_string": "REPLACED",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert f.read_text() == "    return\n    offset = 1\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_different_prefix_suffix_on_same_line(self, tmp_path):
        """Single-line replace: different prefix/suffix both on the target line should work."""
        f = tmp_path / "same-line.txt"
        f.write_text("    return offset + 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "same-line.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "same-line.txt",
                "bounds": [{"line_no": 1, "anchor": "return"}],
                "new_string": "    return value + 1",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "    return value + 1\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_prefix_eq_suffix_duplicate_lines(self, tmp_path):
        """Single-line replace: prefix==suffix with duplicate lines, start_no disambiguates."""
        f = tmp_path / "dup.txt"
        f.write_text("    pass\n    pass\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "dup.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "dup.txt",
                "bounds": [{"line_no": 2, "anchor": "pass"}],
                "new_string": "DONE",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 2
        assert f.read_text() == "    pass\nDONE\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_equidistant_ambiguity_still_errors(self, tmp_path):
        """Single-line replace: equidistant duplicate lines should still report ambiguity."""
        f = tmp_path / "equidistant.txt"
        f.write_text("    pass\n    x = 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "equidistant.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "equidistant.txt",
                "bounds": [{"line_no": 2, "anchor": "pass"}],
                "new_string": "DONE",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "ambiguous" in result.output.lower()
        assert f.read_text() == "    pass\n    x = 1\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_suffix_not_on_prefix_line_errors(self, tmp_path):
        """Single-line replace: suffix found nearby but not on the same line as prefix should error."""
        f = tmp_path / "suffix-wrong-line.txt"
        f.write_text("    return\n    offset = 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "suffix-wrong-line.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "suffix-wrong-line.txt",
                "bounds": [{"line_no": 1, "anchor": "return"}, {"line_no": 1, "anchor": "offset"}],
                "new_string": "REPLACED",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert f.read_text() == "    return\n    offset = 1\n    pass\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_empty_prefix_suffix_on_empty_line(self, tmp_path):
        """Single-line replace: empty prefix/suffix matching an empty line."""
        f = tmp_path / "empty-line.txt"
        f.write_text("top\n\nbody\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "empty-line.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-line.txt",
                "bounds": [{"line_no": 2, "anchor": ""}],
                "new_string": "INSERTED",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 2
        assert f.read_text() == "top\nINSERTED\nbody\n"

    @pytest.mark.asyncio
    async def test_replace_single_line_cross_line_rejection_message(self, tmp_path):
        """Single-line replace: cross-line rejection mentions suffix not on same line."""
        f = tmp_path / "crossline-msg.txt"
        f.write_text("    return\n    offset = 1\n    pass\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "crossline-msg.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "crossline-msg.txt",
                "bounds": [{"line_no": 1, "anchor": "return"}, {"line_no": 1, "anchor": "offset"}],
                "new_string": "REPLACED",
            },
            ctx,
        )

        assert result.metadata.get("error")
        assert "two-bound replace requires different line_no values" in result.output
