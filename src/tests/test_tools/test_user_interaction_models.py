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
from voidx.tools.skills import SkillsTool
from voidx.tools.document import DocumentTool, DocumentInput
from voidx.tools.checkpoint import PlanCheckpointTool
from voidx.agent.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


class TestUserInteractionModels:
    def test_user_interaction_defaults(self):
        ui = UserInteraction(prompt="What?")
        assert ui.options == []
        assert ui.timeout is None

    def test_user_response_cancelled(self):
        resp = UserResponse(value="", cancelled=True)
        assert resp.cancelled is True

    def test_user_interaction_with_options(self):
        ui = UserInteraction(
            prompt="Choose",
            options=["a", "b"],
            timeout=60.0,
        )
        assert len(ui.options) == 2
        assert ui.timeout == 60.0


