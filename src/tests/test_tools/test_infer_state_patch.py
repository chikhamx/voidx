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
from voidx.runtime.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


class TestInferStatePatch:
    @pytest.mark.parametrize(
        ("answer", "expected_intent"),
        [
            ("general", TaskIntent.GENERAL),
            ("coding", TaskIntent.CODING),
            ("chat", TaskIntent.GENERAL),
            ("inspect", TaskIntent.CODING),
            ("design", TaskIntent.CODING),
            ("review", TaskIntent.CODING),
            ("implement", TaskIntent.CODING),
            ("debug", TaskIntent.CODING),
        ],
    )
    def test_intent_match_does_not_set_goal(self, answer, expected_intent):
        response = UserResponse(value=answer)
        patch = _infer_state_patch(response)

        assert patch is not None
        assert patch.intent is not None
        assert patch.intent.type == expected_intent
        assert patch.goal is None
        assert "goal" not in patch.model_fields_set

    def test_intent_match_case_insensitive(self):
        response = UserResponse(value="Implement")
        patch = _infer_state_patch(response)

        assert patch is not None
        assert patch.intent is not None
        assert patch.intent.type == TaskIntent.CODING

    def test_no_match_returns_none(self):
        inp = ClarifyInput(question="What color?")
        response = UserResponse(value="blue")
        patch = _infer_state_patch(response)

        assert patch is None

    def test_empty_answer_returns_none(self):
        inp = ClarifyInput(question="What?")
        response = UserResponse(value="  ")
        patch = _infer_state_patch(response)

        assert patch is None

