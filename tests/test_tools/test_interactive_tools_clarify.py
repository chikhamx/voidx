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
from voidx.tools.file_ops import (
    FileReadInput,
    FileWriteInput,
    FileEditInput,
    EditEntry,
    FileReadTool,
    FileWriteTool,
    FileEditTool,
    _find_paragraph,
)
from voidx.tools.file_state import save_file_version
import voidx.tools.file_state as file_state
from voidx.tools.search import GlobInput, GrepInput
from voidx.tools.bash import BashInput
from voidx.tools.agent import AgentInput, AgentTool
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.todo import TodoInput, TodoWriteTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.clarify import ClarifyTool, ClarifyInput, ClarifyOption, _infer_state_patch
from voidx.tools.load_skills import LoadSkillsTool
from voidx.tools.load_doc_template import LoadDocTemplateTool, LoadDocTemplateInput
from voidx.tools.plan_checkpoint import PlanCheckpointTool
from voidx.agent.task_state import GoalSpec, GoalResolution, GoalType, IntentResolution, PlanResolution, ToolStatePatch
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



class TestInteractiveTools:

    @pytest.mark.asyncio
    async def test_clarify_uses_interaction_callback_and_returns_state_patch(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="implement")

        result = await ClarifyTool().execute(
            {
                "question": "What should I do?",
                "options": [
                    {"label": "Implement", "value": "implement", "description": "Make the change"},
                    {"label": "Inspect", "value": "inspect", "description": "Only inspect"},
                ],
            },
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert requests
        assert result.metadata["clarify_answer"] == "implement"
        assert result.metadata["state_patch"]["intent"]["type"] == "coding"
        assert result.metadata["state_patch"]["goal"]["type"] == "feature"

    @pytest.mark.asyncio
    async def test_plan_checkpoint_approval_sets_implementation_goal(self, tmp_path):
        async def interact(request):
            return UserResponse(value="approved")

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Update runtime state handling"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "approved"
        patch = result.metadata["state_patch"]
        assert patch["intent"]["type"] == "coding"
        assert patch["goal"]["desc"] == "Update runtime state handling"
        assert patch["goal"]["type"] == "feature"
        assert patch["plan"] == {"join": "tdd", "leave": "verify"}
        assert result.next_step_hint == "Plan approved. Proceed to implementation."

    @pytest.mark.asyncio
    async def test_plan_checkpoint_blocks_without_interaction(self, tmp_path):
        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Edit files"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["blocked"] is True
        assert result.metadata["plan_decision"] == "interaction_unavailable"

    @pytest.mark.asyncio
    async def test_clarify_without_interaction_returns_blocked(self, tmp_path):
        result = await ClarifyTool().execute(
            {"question": "What should I do?"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["clarify_cancelled"] is True
        assert result.metadata["blocked"] is True

    @pytest.mark.asyncio
    async def test_clarify_user_cancels(self, tmp_path):
        async def interact(request):
            return UserResponse(value="", cancelled=True)

        result = await ClarifyTool().execute(
            {"question": "What should I do?"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["clarify_cancelled"] is True
        assert "skipped" in result.title

    @pytest.mark.asyncio
    async def test_clarify_free_text_without_options(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="I want to refactor the auth module")

        result = await ClarifyTool().execute(
            {"question": "What would you like to do?"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert len(requests) == 1
        assert requests[0].options == []
        assert result.metadata["clarify_answer"] == "I want to refactor the auth module"

    @pytest.mark.asyncio
    async def test_clarify_free_text_with_options_is_not_selected_option(self, tmp_path):
        async def interact(request):
            return UserResponse(value="Audit the auth flow first", free_text=True)

        result = await ClarifyTool().execute(
            {
                "question": "What should I do?",
                "options": [
                    {"label": "Implement", "value": "implement", "description": "Make the change"},
                    {"label": "Inspect", "value": "inspect", "description": "Only inspect"},
                ],
            },
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        payload = json.loads(result.output)
        assert payload["answer"] == "Audit the auth flow first"
        assert payload["selected_option"] is None

    @pytest.mark.asyncio
    async def test_clarify_passes_context_in_prompt(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="refactor")

        result = await ClarifyTool().execute(
            {
                "question": "What should I do?",
                "context": "This determines the implementation scope",
            },
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert "This determines the implementation scope" in requests[0].prompt

    @pytest.mark.asyncio
    async def test_plan_checkpoint_rejected_keeps_feature_goal_without_hint(self, tmp_path):
        async def interact(request):
            return UserResponse(value="rejected")

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "rejected"
        patch = result.metadata["state_patch"]
        assert patch["intent"]["type"] == "coding"
        assert patch["goal"]["type"] == "feature"
        assert result.next_step_hint == ""

    @pytest.mark.asyncio
    async def test_plan_checkpoint_needs_doc_sets_doc_goal_and_hint(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="needs_doc")

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Add checkpoint document option"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert (
            "Document first",
            "needs_doc",
            "Approve plan and write a design document before implementation",
        ) in requests[0].options
        assert result.metadata["plan_decision"] == "needs_doc"
        patch = result.metadata["state_patch"]
        assert patch["intent"]["type"] == "coding"
        assert patch["goal"]["type"] == "doc"
        assert patch["plan"] == {"join": "design", "leave": "design"}
        assert "design document" in result.next_step_hint

    @pytest.mark.asyncio
    async def test_plan_checkpoint_modified_updates_scope(self, tmp_path):
        interact_calls = []

        async def interact(request):
            interact_calls.append(request)
            if len(interact_calls) == 1:
                return UserResponse(value="modified")
            return UserResponse(value="Only refactor the login function")

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "modified"
        patch = result.metadata["state_patch"]
        assert patch["intent"]["type"] == "coding"
        assert patch["goal"]["desc"] == "Only refactor the login function"
        assert patch["goal"]["type"] == "feature"
        assert result.next_step_hint == ""
        assert len(interact_calls) == 2
        assert "Describe the modified scope" in interact_calls[1].prompt

    @pytest.mark.asyncio
    async def test_plan_checkpoint_free_text_is_modified_not_approved(self, tmp_path):
        async def interact(request):
            return UserResponse(value="Only update the login form", free_text=True)

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "modified"
        payload = json.loads(result.output)
        assert payload["decision"] == "modified"
        assert payload["modified_scope"] == "Only update the login form"
        patch = result.metadata["state_patch"]
        assert patch["intent"]["type"] == "coding"
        assert patch["goal"]["desc"] == "Only update the login form"

    @pytest.mark.asyncio
    async def test_plan_checkpoint_modified_scope_cancelled_falls_back_to_summary(self, tmp_path):
        async def interact(request):
            if request.options:
                return UserResponse(value="modified")
            return UserResponse(value="", cancelled=True)

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "modified"
        patch = result.metadata["state_patch"]
        assert patch["goal"]["desc"] == "Refactor auth module"

    @pytest.mark.asyncio
    async def test_plan_checkpoint_user_cancels_treated_as_rejected(self, tmp_path):
        async def interact(request):
            return UserResponse(value="", cancelled=True)

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "rejected"

    @pytest.mark.asyncio
    async def test_clarify_sets_default_timeout_on_interaction(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="chat")

        result = await ClarifyTool().execute(
            {"question": "What should I do?"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert requests[0].timeout == 120.0

    @pytest.mark.asyncio
    async def test_plan_checkpoint_sets_default_timeout_on_interaction(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="approved")

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert requests[0].timeout == 120.0

    def test_plan_checkpoint_description_mentions_document_first(self):
        assert (
            "design document" in PlanCheckpointTool.description
            or "Document first" in PlanCheckpointTool.description
        )


