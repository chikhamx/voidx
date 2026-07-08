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


class TestToolStatePatch:
    def test_model_fields_set_tracks_explicit_fields(self):
        patch = ToolStatePatch(intent=IntentResolution(type=TaskIntent.CODING))
        assert "intent" in patch.model_fields_set
        assert "goal" not in patch.model_fields_set

    def test_full_patch_round_trips(self):
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc="Refactor auth"),
        )
        data = patch.model_dump(mode="json")
        restored = ToolStatePatch.model_validate(data)
        assert restored.intent is not None
        assert restored.intent.type == TaskIntent.CODING
        assert restored.goal is not None
        assert restored.goal.desc == "Refactor auth"


