"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file import FileReadInput, FileReadTool
from voidx.tools.file.state import save_file_version
import voidx.tools.file.state as file_state
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
