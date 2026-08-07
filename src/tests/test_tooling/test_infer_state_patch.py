"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.application.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.interaction import (
    UserInteraction,
    UserResponse,
)
from voidx.tooling.builtin.file import FileReadInput, FileReadTool
from voidx.tooling.adapters.persistence.file_snapshot import save_file_version
import voidx.tooling.application.file_state as file_state
from voidx.tooling.builtin.file.search import FindInput, SearchInput
from voidx.tooling.builtin.shell.bash import BashInput
from voidx.agent.adapters.tools.subagent import AgentInput, AgentTool
from voidx.agent.application.runtime.task_tracker import TaskTracker
from voidx.agent.adapters.tools.todo import TodoInput, TodoWriteTool
from voidx.tooling.application.registry import ToolRegistry
from voidx.agent.adapters.tools.interaction.clarify import ClarifyTool, ClarifyInput, _infer_state_patch
from voidx.tooling.adapters.skills import SkillsTool
from voidx.tooling.builtin.document import DocumentTool, DocumentInput
from voidx.agent.adapters.tools.interaction.checkpoint import PlanCheckpointTool
from voidx.agent.domain.task.state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.application.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.agent.domain.automation.workflow import WorkflowStateEventKind
import voidx.persistence.sqlite as store


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

