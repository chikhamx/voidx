"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file_ops import FileReadInput, FileReadTool
from voidx.tools.file_ops.write import FileWriteInput, FileWriteTool
from voidx.tools.file_ops.edit_execute import FileEditInput, FileEditTool
from voidx.tools.file_ops.types import EditEntry
from voidx.tools.file_ops.edit_resolve import _find_paragraph
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



class TestLoadDocTemplate:
    @pytest.mark.asyncio
    async def test_load_valid_template(self, tmp_path):
        tool = LoadDocTemplateTool()
        ctx = ToolContext(workspace=str(tmp_path))
        for doc_type in ("prd", "tech-design", "rfc", "api-doc", "readme"):
            result = await tool.execute({"doc_type": doc_type}, ctx)
            assert result.title == f"Template: {doc_type}"
            assert len(result.output) > 50
            assert result.metadata["doc_type"] == doc_type

    @pytest.mark.asyncio
    async def test_invalid_doc_type(self, tmp_path):
        tool = LoadDocTemplateTool()
        ctx = ToolContext(workspace=str(tmp_path))
        result = await tool.execute({"doc_type": "nonexistent"}, ctx)
        assert "Unknown doc_type" in result.output
        assert "nonexistent" in result.output

    @pytest.mark.asyncio
    async def test_case_insensitive(self, tmp_path):
        tool = LoadDocTemplateTool()
        ctx = ToolContext(workspace=str(tmp_path))
        result = await tool.execute({"doc_type": "PRD"}, ctx)
        assert result.title == "Template: prd"

    @pytest.mark.asyncio
    async def test_input_schema(self):
        schema = LoadDocTemplateInput.model_json_schema()
        assert "doc_type" in schema["properties"]
