"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file_ops import FileReadInput, FileReadTool
from voidx.tools.file_state import save_file_version
import voidx.tools.file_state as file_state
from voidx.tools.search import GlobInput, GrepInput
from voidx.tools.bash import BashInput
from voidx.tools.agent import AgentInput, AgentTool
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.todo import TodoInput, TodoWriteTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.clarify import ClarifyTool, ClarifyInput, _infer_state_patch
from voidx.tools.load_skills import LoadSkillsTool
from voidx.tools.load_doc_template import LoadDocTemplateTool, LoadDocTemplateInput
from voidx.tools.plan_checkpoint import PlanCheckpointTool
from voidx.agent.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


def _replace(lineno: int, prefix: str, suffix: str | None = None, new_string: str = "") -> dict:
    return {
        "operation": "replace",
        "lineno": lineno,
        "prefix": prefix,
        "suffix": prefix if suffix is None else suffix,
        "new_string": new_string,
    }


def _insert(lineno: int, prefix: str, suffix: str | None = None, new_string: str = "") -> dict:
    return {
        "operation": "insert",
        "lineno": lineno,
        "prefix": prefix,
        "suffix": prefix if suffix is None else suffix,
        "new_string": new_string,
    }


def _insert_bof(new_string: str) -> dict:
    return {"operation": "insert", "lineno": 0, "prefix": "", "suffix": "", "new_string": new_string}



class TestTaskTracker:
    """TaskTracker reports worker-persona progress."""

    def test_start_and_update(self):
        tracker = TaskTracker()
        tracker.start("t1", "implement", "write foo.py")
        t = tracker.get("t1")
        assert t is not None
        assert t.status == "running"
        assert t.agent == "implement"

        tracker.update("t1", last_output="writing file...")
        t = tracker.get("t1")
        assert "writing file" in t.last_output

    def test_finish(self):
        tracker = TaskTracker()
        tracker.start("t2", "explore", "search")
        tracker.finish("t2", "completed")
        assert tracker.get("t2").status == "completed"

    def test_list_running(self):
        tracker = TaskTracker()
        tracker.start("a", "explore", "x")
        tracker.start("b", "implement", "y")
        tracker.finish("a", "completed")
        running = tracker.list_running()
        assert len(running) == 1
        assert running[0].id == "b"

    def test_format_status(self):
        tracker = TaskTracker()
        tracker.start("t1", "implement", "write foo.py")
        tracker.update("t1", last_output="found target")
        output = tracker.format_status()
        assert "implement" in output
        assert "running" in output

    def test_todo_state_is_managed_through_public_api(self):
        tracker = TaskTracker()
        todos = [{"id": "fix", "content": "ship fix", "status": "pending"}]

        tracker.set_todos([SimpleNamespace(id="fix", content="ship fix", status="pending")])
        todos.clear()

        assert tracker.list_todos() == [{"id": "fix", "content": "ship fix", "status": "pending"}]
        tracker.clear_todos()
        assert tracker.list_todos() == []

    @pytest.mark.asyncio
    async def test_todo_tool_returns_structured_metadata(self, tmp_path):
        tracker = TaskTracker()
        tool = TodoWriteTool(tracker=tracker)
        ctx = ToolContext(workspace=str(tmp_path))

        result = await tool.execute({
            "todos": [
                {"id": "impl", "content": "implement event", "status": "in_progress"},
                {"id": "test", "content": "write tests", "status": "pending"},
                {"id": "docs", "content": "update docs", "status": "completed"},
            ],
        }, ctx)

        assert result.metadata["todo_summary"] == "1/3 done · 1 active · 1 pending"
        assert result.metadata["todo_items"] == [
            {"id": "impl", "content": "implement event", "status": "in_progress"},
            {"id": "test", "content": "write tests", "status": "pending"},
            {"id": "docs", "content": "update docs", "status": "completed"},
        ]
        assert tracker.list_todos()[0]["content"] == "implement event"

    def test_todo_input_rejects_unknown_status(self):
        with pytest.raises(ValueError):
            TodoInput.model_validate({
                "todos": [{"id": "bad", "content": "bad status", "status": "blocked"}],
            })

    @pytest.mark.asyncio
    async def test_task_status_tool(self, tmp_path):
        tracker = TaskTracker()
        tracker.start("t1", "explore", "scan directory")
        tool = TaskStatusTool(tracker=tracker)
        ctx = ToolContext(workspace=str(tmp_path))

        result = await tool.execute({}, ctx)
        assert "explore" in result.output
        assert "running" in result.output

        result2 = await tool.execute({"task_id": "t1"}, ctx)
        assert "t1" in result2.output


