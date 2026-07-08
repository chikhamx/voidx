"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage
from pydantic import ValidationError

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
from voidx.ui.output.events import (
    ClarifyAnswerSubmitted,
    ClarifyPromptShown,
)
from voidx.tools.load_skills import LoadSkillsTool
from voidx.tools.load_doc_template import LoadDocTemplateTool, LoadDocTemplateInput
from voidx.tools.plan_checkpoint import PlanCheckpointInput, PlanCheckpointTool, _build_prompt
from voidx.ui.output.events import (
    CheckpointDecisionSubmitted,
    CheckpointPromptShown,
    ui_events,
)
from voidx.agent.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


class TestClarifyTool:

    @pytest.mark.asyncio
    async def test_clarify_uses_interaction_callback_and_returns_state_patch(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="implement")

        result = await ClarifyTool().execute(
            {
                "question": "What should I do?",
                "options": ["implement", "inspect"],
            },
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert requests
        assert result.metadata["clarify_answer"] == "implement"
        assert result.metadata["state_patch"]["intent"]["type"] == "coding"
        assert result.metadata["state_patch"]["goal"]["desc"] == "implement"


    @pytest.mark.asyncio
    async def test_clarify_emits_prompt_and_answer_events(self, tmp_path):
        events = []

        class RecordingConsumer:
            def handle(self, event):
                events.append(event)

        if ui_events.is_running:
            await ui_events.stop()
        ui_events.start(RecordingConsumer())
        try:
            async def interact(request):
                return UserResponse(value="implement")

            result = await ClarifyTool().execute(
                {
                    "question": "What should I do?",
                    "options": ["implement", "inspect"],
                },
                ToolContext(workspace=str(tmp_path), interact=interact),
            )
        finally:
            await ui_events.stop()

        shown = next(event for event in events if isinstance(event, ClarifyPromptShown))
        submitted = next(event for event in events if isinstance(event, ClarifyAnswerSubmitted))

        assert result.metadata["clarify_answer"] == "implement"
        assert shown.clarify_id == submitted.clarify_id
        assert shown.question == "What should I do?"
        assert shown.options == ["implement", "inspect"]
        assert submitted.answer == "implement"
        assert submitted.cancelled is False
        assert submitted.was_custom_input is True

    @pytest.mark.asyncio
    async def test_clarify_passes_empty_options_when_event_ui_is_active(self, tmp_path):
        events = []
        requests = []

        class RecordingConsumer:
            def handle(self, event):
                events.append(event)

        if ui_events.is_running:
            await ui_events.stop()
        ui_events.start(RecordingConsumer())
        try:
            async def interact(request):
                requests.append(request)
                return UserResponse(value="implement")

            await ClarifyTool().execute(
                {
                    "question": "What should I do?",
                    "options": ["implement", "inspect"],
                },
                ToolContext(workspace=str(tmp_path), interact=interact),
            )
        finally:
            await ui_events.stop()

        shown = next(event for event in events if isinstance(event, ClarifyPromptShown))
        assert shown.options == ["implement", "inspect"]
        assert requests[0].prompt == "Question:"
        assert requests[0].options == []

    @pytest.mark.asyncio
    async def test_clarify_passes_full_options_when_event_ui_inactive(self, tmp_path):
        requests = []

        if ui_events.is_running:
            await ui_events.stop()

        async def interact(request):
            requests.append(request)
            return UserResponse(value="implement")

        await ClarifyTool().execute(
            {
                "question": "What should I do?",
                "options": ["implement", "inspect"],
            },
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert requests[0].prompt == "What should I do?"
        assert requests[0].options == ["implement", "inspect"]

    @pytest.mark.asyncio
    async def test_clarify_emits_cancelled_answer_event_on_skip(self, tmp_path):
        events = []

        class RecordingConsumer:
            def handle(self, event):
                events.append(event)

        if ui_events.is_running:
            await ui_events.stop()
        ui_events.start(RecordingConsumer())
        try:
            async def interact(request):
                return UserResponse(value="", cancelled=True)

            result = await ClarifyTool().execute(
                {"question": "What should I do?", "options": []},
                ToolContext(workspace=str(tmp_path), interact=interact),
            )
        finally:
            await ui_events.stop()

        submitted = next(event for event in events if isinstance(event, ClarifyAnswerSubmitted))
        assert submitted.cancelled is True
        assert result.metadata["clarify_cancelled"] is True

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
                "options": ["implement", "inspect"],
            },
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        payload = json.loads(result.output)
        assert payload["answer"] == "Audit the auth flow first"

    @pytest.mark.asyncio
    async def test_clarify_interaction_unavailable_has_summary(self, tmp_path):
        """interaction_unavailable path should have a non-empty summary."""
        result = await ClarifyTool().execute(
            {"question": "What should I do?"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata.get("clarify_cancelled") is True
        assert result.metadata.get("blocked") is True
        assert result.summary is not None
        assert len(result.summary) > 0
        assert "unavailable" in result.summary

    @pytest.mark.asyncio
    async def test_clarify_invalid_arguments_has_summary(self, tmp_path):
        """Invalid arguments path should have a non-empty summary."""
        result = await ClarifyTool().execute(
            {},  # missing required 'question' field
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata.get("error") is True
        assert result.summary is not None
        assert len(result.summary) > 0
        assert "invalid" in result.summary

    @pytest.mark.asyncio
    async def test_clarify_skipped_has_summary(self, tmp_path):
        """User skipped path should have a non-empty summary."""
        async def interact(request):
            return UserResponse(value="", cancelled=True)

        result = await ClarifyTool().execute(
            {"question": "What should I do?"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata.get("clarify_cancelled") is True
        assert result.summary is not None
        assert len(result.summary) > 0
        assert "skipped" in result.summary
