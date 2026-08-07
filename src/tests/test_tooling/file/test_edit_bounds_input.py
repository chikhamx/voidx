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


class TestReplaceBoundsInput:
    def test_parameter_descriptions_explain_bounds_shape_without_drift(self):
        schema = FileReplaceTool().parameters_schema()
        properties = schema["properties"]

        assert "bounds" in properties
        assert "start_no" not in properties
        assert "end_no" not in properties
        assert "One or two boundary locators" in properties["bounds"]["description"]
        assert "inclusive range" in properties["bounds"]["description"]
        assert "Length must be 1 or 2" in properties["bounds"]["description"]
        assert "unchanged surrounding lines" in properties["new_string"]["description"]
        visible = "\n".join(prop.get("description", "") for prop in properties.values())
        assert "drift" not in visible.lower()

    @pytest.mark.asyncio
    async def test_replace_accepts_reversed_two_bounds(self, tmp_path):
        f = tmp_path / "reverse-bounds.txt"
        f.write_text("top\nbody\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "reverse-bounds.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "reverse-bounds.txt",
                "bounds": [
                    {"line_no": 3, "anchor": "end"},
                    {"line_no": 2, "anchor": "body"},
                ],
                "new_string": "replacement",
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert result.metadata["start_line"] == 2
        assert result.metadata["end_line"] == 3
        assert f.read_text() == "top\nreplacement\n"

    @pytest.mark.asyncio
    async def test_replace_rejects_multi_line_empty_anchor_before_resolver(self, tmp_path):
        f = tmp_path / "empty-bound.txt"
        f.write_text("top\nbody\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "empty-bound.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "empty-bound.txt",
                "bounds": [
                    {"line_no": 2, "anchor": ""},
                    {"line_no": 3, "anchor": "end"},
                ],
                "new_string": "replacement",
            },
            ctx,
        )

        assert result.metadata.get("error") is True
        assert "multi-line replace requires non-empty anchors" in result.output
        assert "empty line" not in result.output
        assert f.read_text() == "top\nbody\nend\n"

    @pytest.mark.asyncio
    async def test_replace_rejects_duplicate_two_bound_line_numbers(self, tmp_path):
        f = tmp_path / "duplicate-bound.txt"
        f.write_text("top\nbody\nend\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = build_registry()
        await r.execute_tool("read", {"file_path": "duplicate-bound.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {
                "file_path": "duplicate-bound.txt",
                "bounds": [{"line_no": 2, "anchor": "body"}, {"line_no": 2, "anchor": "body"}],
                "new_string": "replacement",
            },
            ctx,
        )

        assert result.metadata.get("error") is True
        assert "two-bound replace requires different line_no values" in result.output
