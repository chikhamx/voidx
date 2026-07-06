"""Tests for file edit operations — replace and line insert via registry."""

import sys
from pathlib import Path


import pytest

from voidx.tools.base import ToolContext
from voidx.tools.file_ops.edit_execute import FileReplaceTool
from voidx.tools.file_ops.edit_resolve import _find_text_segment
from voidx.tools.registry import ToolRegistry
import voidx.tools.file_state as file_state


class TestFileOpsTrailingNewline:
    async def test_replace_trailing_newline_does_not_corrupt_next_line(self, tmp_path):
        """Regression: new_string ending with \\n must not insert a blank line before the next line."""
        f = tmp_path / "trailing.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "trailing.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "trailing.txt", "bounds": [{"line_no": 2, "anchor": "line2"}], "new_string": "NEW2\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nNEW2\nline3\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_multiline_does_not_corrupt_next_line(self, tmp_path):
        """Regression: multi-line new_string ending with \\n must not insert blank line."""
        f = tmp_path / "multi-trailing.txt"
        f.write_text("line1\nline2\nline3\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-trailing.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "multi-trailing.txt", "bounds": [{"line_no": 2, "anchor": "line2"}, {"line_no": 3, "anchor": "line3"}], "new_string": "NEW_A\nNEW_B\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nNEW_A\nNEW_B\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_on_last_line(self, tmp_path):
        """Regression: replacing last line with trailing-newline new_string must not add extra blank line."""
        f = tmp_path / "last-line.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "last-line.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "last-line.txt", "bounds": [{"line_no": 3, "anchor": "line3"}], "new_string": "NEW3\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nline2\nNEW3\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_no_trailing_newline_file(self, tmp_path):
        """File without trailing newline: new_string ending with \\n must not add extra blank line."""
        f = tmp_path / "no-trailing.txt"
        f.write_text("line1\nline2\nline3")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "no-trailing.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "no-trailing.txt", "bounds": [{"line_no": 2, "anchor": "line2"}], "new_string": "NEW2\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nNEW2\nline3"

    @pytest.mark.asyncio
    async def test_replace_no_trailing_newline_preserves_next_line(self, tmp_path):
        """new_string without trailing \\n: next line must be untouched (baseline)."""
        f = tmp_path / "baseline.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "baseline.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "baseline.txt", "bounds": [{"line_no": 2, "anchor": "line2"}], "new_string": "NEW2"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nNEW2\nline3\n"

    @pytest.mark.asyncio
    async def test_replace_double_trailing_newline_intentional_blank_line(self, tmp_path):
        """new_string ending with \\n\\n (intentional blank line after replacement)."""
        f = tmp_path / "double-newline.txt"
        f.write_text("line1\nline2\nline3\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "double-newline.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "double-newline.txt", "bounds": [{"line_no": 2, "anchor": "line2"}], "new_string": "NEW2\n\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nNEW2\n\nline3\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_python_code(self, tmp_path):
        """Realistic: replace function body in Python code with trailing newline."""
        f = tmp_path / "code.py"
        f.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "code.py"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "code.py", "bounds": [{"line_no": 1, "anchor": "def foo():"}, {"line_no": 2, "anchor": "return 1"}], "new_string": "def foo():\n    return 42\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "def foo():\n    return 42\n\ndef bar():\n    return 2\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_multiline_to_single_line(self, tmp_path):
        """Replace multi-line segment with single line ending in \\n."""
        f = tmp_path / "multi-to-single.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "multi-to-single.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "multi-to-single.txt", "bounds": [{"line_no": 2, "anchor": "line2"}, {"line_no": 4, "anchor": "line4"}], "new_string": "REPLACED\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nREPLACED\nline5\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_single_line_file(self, tmp_path):
        """Single-line file: replace with trailing-newline new_string."""
        f = tmp_path / "single.txt"
        f.write_text("only_line\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "single.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "single.txt", "bounds": [{"line_no": 1, "anchor": "only_line"}], "new_string": "REPLACED\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "REPLACED\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_two_line_file(self, tmp_path):
        """Two-line file: replace first line with trailing-newline new_string."""
        f = tmp_path / "two.txt"
        f.write_text("line1\nline2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "two.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "two.txt", "bounds": [{"line_no": 1, "anchor": "line1"}], "new_string": "NEW1\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "NEW1\nline2\n"

    @pytest.mark.asyncio
    async def test_replace_sequential_trailing_newline_replaces(self, tmp_path):
        """Two sequential replaces both with trailing newline: no cumulative blank lines."""
        f = tmp_path / "sequential.txt"
        f.write_text("line1\nline2\nline3\nline4\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "sequential.txt"}, ctx)

        r1 = await r.execute_tool(
            "replace",
            {"file_path": "sequential.txt", "bounds": [{"line_no": 2, "anchor": "line2"}], "new_string": "NEW2\n"},
            ctx,
        )
        assert r1.metadata.get("error") is not True

        # Read again to update coverage after first edit
        await r.execute_tool("read", {"file_path": "sequential.txt"}, ctx)

        r2 = await r.execute_tool(
            "replace",
            {"file_path": "sequential.txt", "bounds": [{"line_no": 3, "anchor": "line3"}], "new_string": "NEW3\n"},
            ctx,
        )
        assert r2.metadata.get("error") is not True

        assert f.read_text() == "line1\nNEW2\nNEW3\nline4\n"

    @pytest.mark.asyncio
    async def test_replace_new_string_with_leading_and_trailing_newline(self, tmp_path):
        """new_string starting with \\n and ending with \\n: intentional blank line before and after."""
        f = tmp_path / "lead-trail.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "lead-trail.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "lead-trail.txt", "bounds": [{"line_no": 2, "anchor": "line2"}], "new_string": "\nNEW2\n"},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\n\nNEW2\nline3\n"

    @pytest.mark.asyncio
    async def test_replace_trailing_newline_replace_with_empty(self, tmp_path):
        """Delete a line (empty new_string): next line must not be corrupted."""
        f = tmp_path / "delete.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "delete.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "delete.txt", "bounds": [{"line_no": 2, "anchor": "line2"}], "new_string": ""},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nline3\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["\n", " "])
    async def test_replace_single_line_delete_normalizes_blank_new_string(self, tmp_path, blank):
        """Single-line delete with blank new_string (\\n or space) should remove the line, not leave an empty line."""
        f = tmp_path / "delete-blank.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "delete-blank.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "delete-blank.txt", "bounds": [{"line_no": 2, "anchor": "line2"}], "new_string": blank},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nline3\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["\n", " "])
    async def test_replace_single_line_delete_normalizes_blank_new_string_empty_anchor(self, tmp_path, blank):
        """Single-line delete with blank new_string and empty anchors should also remove the line."""
        f = tmp_path / "delete-blank-empty-anchor.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        await r.execute_tool("read", {"file_path": "delete-blank-empty-anchor.txt"}, ctx)

        result = await r.execute_tool(
            "replace",
            {"file_path": "delete-blank-empty-anchor.txt", "bounds": [{"line_no": 2, "anchor": ""}], "new_string": blank},
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert f.read_text() == "line1\nline3\n"
