"""Tests for TodoWriteTool failure-path summaries."""

import sys
from pathlib import Path


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


    @pytest.mark.asyncio
    async def test_todo_update_empty_content_does_not_clear_existing(self, tmp_path):
        """Strict-schema models often pass content='' for status-only updates."""
        from voidx.tools.task_tracker import TaskTracker

        tracker = TaskTracker()
        tracker.set_todos_from_dict(
            {
                "impl": {"content": "implement event", "status": "active"},
                "test": {"content": "write tests", "status": "pending"},
            }
        )
        tool = TodoWriteTool(tracker=tracker)
        ctx = ToolContext(workspace=str(tmp_path))

        result = await tool.execute(
            {
                "op": "update",
                "updates": [
                    {"id": "impl", "status": "done", "content": ""},
                    {"id": "test", "status": "active", "content": ""},
                ],
            },
            ctx,
        )

        assert result.metadata.get("error") is not True
        assert tracker.get_todos() == {
            "impl": {"content": "implement event", "status": "done"},
            "test": {"content": "write tests", "status": "active"},
        }
        assert result.metadata["todo_items"] == [
            {"id": "impl", "content": "implement event", "status": "done"},
            {"id": "test", "content": "write tests", "status": "active"},
        ]
