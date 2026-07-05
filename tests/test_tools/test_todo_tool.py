"""Tests for TodoWriteTool failure-path summaries."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.todo import TodoWriteTool


class TestTodoTool:

    @pytest.mark.asyncio
    async def test_todo_invalid_arguments_has_summary(self, tmp_path):
        """Invalid arguments path should have a non-empty summary."""
        result = await TodoWriteTool().execute(
            {"op": 123},  # invalid type for 'op'
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata.get("error") is True
        assert result.summary is not None
        assert len(result.summary) > 0
        assert "invalid" in result.summary
