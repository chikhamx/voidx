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

from voidx.agent.application.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from tests.agent_tool_context import agent_tool_context as ToolContext
from voidx.agent.adapters.tools.context import AgentToolRuntime
from voidx.presentation.output.tool_events import PresentationToolUiEventPublisher
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
from voidx.presentation.output.events import (
    ClarifyAnswerSubmitted,
    ClarifyPromptShown,
)
from voidx.tooling.adapters.skills import SkillsTool
from voidx.tooling.builtin.document import DocumentTool, DocumentInput
from voidx.agent.adapters.tools.interaction.checkpoint import PlanCheckpointInput, PlanCheckpointTool, _build_prompt
from voidx.presentation.output.events import (
    CheckpointDecisionSubmitted,
    CheckpointPromptShown,
    ui_events,
)
from voidx.agent.domain.task.state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.application.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.agent.domain.automation.workflow import WorkflowStateEventKind
import voidx.persistence.sqlite as store


class TestPlanCheckpoint:

    async def test_plan_checkpoint_approval_sets_implementation_goal(self, tmp_path):
        async def interact(request):
            return UserResponse(value="approved")

        result = await PlanCheckpointTool().execute(
            {"goal": "Update runtime state handling"},
            ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
        )

        assert result.metadata["plan_decision"] == "approved"
        patch = result.metadata["state_patch"]
        assert patch["intent"]["type"] == "coding"
        assert patch["goal"]["desc"] == "Update runtime state handling"
        assert patch["plan"] == {"join": "tdd", "leave": "verify"}
        assert patch["workflow_runs"][0]["name"] == "tdd"
        assert patch["workflow_runs"][0]["status"] == "active"
        assert result.next_step_hint == ""

    @pytest.mark.asyncio
    async def test_plan_checkpoint_emits_voidx_plan_events(self, tmp_path):
        events = []

        class RecordingConsumer:
            def handle(self, event):
                events.append(event)

        if ui_events.is_running:
            await ui_events.stop()
        ui_events.start(RecordingConsumer())
        try:
            async def interact(request):
                return UserResponse(value="approved")

            result = await PlanCheckpointTool().execute(
                {
                    "goal": "Add checkpoint node",
                    "steps": ["Add event schema"],
                    "affected_files": ["src/voidx/tools/plan_checkpoint.py"],
                    "risks": ["Avoid duplicate JSON"],
                },
                ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
            )
        finally:
            await ui_events.stop()

        shown = next(event for event in events if isinstance(event, CheckpointPromptShown))
        submitted = next(event for event in events if isinstance(event, CheckpointDecisionSubmitted))

        assert result.metadata["plan_decision"] == "approved"
        assert shown.checkpoint_id == submitted.checkpoint_id
        assert shown.plan.goal == "Add checkpoint node"
        assert shown.plan.steps == ["Add event schema"]
        assert submitted.decision == "approved"
        assert submitted.label == "Implement directly"
        assert submitted.response == "Implement directly"
        assert submitted.was_custom_input is False

    @pytest.mark.asyncio
    async def test_plan_checkpoint_uses_short_choice_prompt_when_event_ui_is_active(self, tmp_path):
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
                return UserResponse(value="approved")

            await PlanCheckpointTool().execute(
                {
                    "goal": "Add checkpoint node",
                    "steps": ["Add event schema"],
                    "affected_files": ["src/voidx/tools/plan_checkpoint.py"],
                    "risks": ["Avoid duplicate JSON"],
                },
                ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
            )
        finally:
            await ui_events.stop()

        shown = next(event for event in events if isinstance(event, CheckpointPromptShown))

        assert shown.plan.goal == "Add checkpoint node"
        assert requests[0].prompt == "Plan:"

    def test_plan_checkpoint_prompt_renders_flat_steps_and_scope_details(self):
        prompt = _build_prompt(PlanCheckpointInput(
            goal="Simplify checkpoint input",
            steps=[
                "Replace nested step objects with strings",
                "Update schema tests",
            ],
            affected_files=[
                "src/voidx/tools/plan_checkpoint.py",
                "tests/test_tools/test_tool_registry.py",
            ],
            risks=[
                "Old object-shaped steps should fail validation",
            ],
        ))

        assert "Goal: Simplify checkpoint input" in prompt
        assert "1. Replace nested step objects with strings" in prompt
        assert "2. Update schema tests" in prompt
        assert (
            "Affected files: src/voidx/tools/plan_checkpoint.py, "
            "tests/test_tools/test_tool_registry.py"
        ) in prompt
        assert "- Old object-shaped steps should fail validation" in prompt
        assert "Alternatives:" not in prompt
        assert "Estimated steps:" not in prompt

    def test_plan_checkpoint_rejects_legacy_object_steps(self):
        with pytest.raises(ValidationError):
            PlanCheckpointInput.model_validate({
                "goal": "Simplify checkpoint input",
                "steps": [
                    {
                        "description": "Update the model",
                        "files": ["src/voidx/tools/plan_checkpoint.py"],
                        "tool": "edit",
                    },
                ],
            })

    @pytest.mark.asyncio
    async def test_plan_checkpoint_approval_satisfies_active_workflow_and_activates_tdd(self, tmp_path):
        async def interact(request):
            return UserResponse(value="approved")

        result = await PlanCheckpointTool().execute(
            {"goal": "Fix runtime state handling"},
            ToolContext(
                workspace=str(tmp_path),
                runtime=AgentToolRuntime(interaction=interact),
                workflow_runs=[
                    WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE),
                ],
                turn_count=7,
            ),
        )

        patch = result.metadata["state_patch"]
        by_name = {run["name"]: run for run in patch["workflow_runs"]}
        assert by_name["debug"]["status"] == "satisfied"
        assert by_name["debug"]["updated_turn"] == 7
        assert by_name["tdd"]["status"] == "active"
        assert by_name["tdd"]["activated_turn"] == 7
        assert by_name["tdd"]["updated_turn"] == 7
        assert by_name["tdd"]["personas"] == ["implement"]

    @pytest.mark.asyncio
    async def test_plan_checkpoint_workflow_patch_does_not_stop_turn(self, tmp_path):
        from langchain_core.messages import ToolMessage

        from voidx.agent.adapters.langgraph.runtime.tool_executor import _ExecutedTool, _state_update_from_executed_tools

        async def interact(request):
            return UserResponse(value="approved")

        checkpoint = await PlanCheckpointTool().execute(
            {"goal": "Fix runtime state handling"},
            ToolContext(
                workspace=str(tmp_path),
                runtime=AgentToolRuntime(interaction=interact),
                workflow_runs=[
                    WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE),
                ],
            ),
        )
        executed = [_ExecutedTool(
            message=ToolMessage(content=checkpoint.output, tool_call_id="call_checkpoint"),
            result=checkpoint,
            tool_call={"name": "checkpoint"},
        )]

        update = _state_update_from_executed_tools(
            executed,
            current_workflow_runs=[
                WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE),
            ],
        )

        by_name = {run.name: run for run in update["workflow_runs"]}
        assert by_name["debug"].status == WorkflowRunStatus.SATISFIED
        assert by_name["tdd"].status == WorkflowRunStatus.ACTIVE
        assert update.get("should_continue") is not False

    @pytest.mark.asyncio
    async def test_plan_checkpoint_blocks_without_interaction(self, tmp_path):
        result = await PlanCheckpointTool().execute(
            {"goal": "Edit files"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["blocked"] is True
        assert result.metadata["plan_decision"] == "interaction_unavailable"

    @pytest.mark.asyncio

    async def test_plan_checkpoint_rejected_does_not_update_goal(self, tmp_path):
        async def interact(request):
            return UserResponse(value="rejected")

        result = await PlanCheckpointTool().execute(
            {"goal": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
        )

        assert result.metadata["plan_decision"] == "rejected"
        patch = result.metadata["state_patch"]
        assert patch["intent"]["type"] == "coding"
        assert "goal" not in patch
        assert result.next_step_hint == ""

    @pytest.mark.asyncio
    async def test_plan_checkpoint_needs_doc_sets_doc_goal_and_hint(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="needs_doc")

        result = await PlanCheckpointTool().execute(
            {"goal": "Add checkpoint document option"},
            ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
        )

        assert any(label == "Document first" for label, _, _ in requests[0].options)
        assert result.metadata["plan_decision"] == "needs_doc"
        patch = result.metadata["state_patch"]
        assert patch["intent"]["type"] == "coding"
        assert patch["goal"]["desc"] == "Add checkpoint document option"
        assert patch["plan"] == {"join": "design", "leave": "design"}
        assert result.next_step_hint == ""

    @pytest.mark.asyncio
    async def test_plan_checkpoint_needs_doc_satisfies_active_workflow_and_activates_design(self, tmp_path):
        async def interact(request):
            return UserResponse(value="needs_doc")

        result = await PlanCheckpointTool().execute(
            {"goal": "Document runtime state handling"},
            ToolContext(
                workspace=str(tmp_path),
                runtime=AgentToolRuntime(interaction=interact),
                workflow_runs=[
                    WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE),
                ],
            ),
        )

        patch = result.metadata["state_patch"]
        by_name = {run["name"]: run for run in patch["workflow_runs"]}
        assert by_name["debug"]["status"] == "satisfied"
        assert by_name["design"]["status"] == "active"
        assert by_name["design"]["personas"] == ["plan"]

    @pytest.mark.asyncio
    async def test_plan_checkpoint_modified_updates_scope(self, tmp_path):
        interact_calls = []

        async def interact(request):
            interact_calls.append(request)
            if len(interact_calls) == 1:
                return UserResponse(value="modified")
            return UserResponse(value="Only refactor the login function")

        result = await PlanCheckpointTool().execute(
            {"goal": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
        )

        assert result.metadata["plan_decision"] == "modified"
        patch = result.metadata["state_patch"]
        assert patch["intent"]["type"] == "coding"
        assert patch["goal"]["desc"] == "Only refactor the login function"
        assert result.next_step_hint == ""
        assert len(interact_calls) == 2
        assert "Describe the modified scope" in interact_calls[1].prompt

    @pytest.mark.asyncio
    async def test_plan_checkpoint_free_text_is_modified_not_approved(self, tmp_path):
        async def interact(request):
            return UserResponse(value="Only update the login form", free_text=True)

        result = await PlanCheckpointTool().execute(
            {"goal": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
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
            {"goal": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
        )

        assert result.metadata["plan_decision"] == "modified"
        patch = result.metadata["state_patch"]
        assert patch["goal"]["desc"] == "Refactor auth module"

    @pytest.mark.asyncio
    async def test_plan_checkpoint_user_cancels_treated_as_rejected(self, tmp_path):
        async def interact(request):
            return UserResponse(value="", cancelled=True)

        result = await PlanCheckpointTool().execute(
            {"goal": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
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
            ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
        )

        assert requests[0].timeout == 120.0

    @pytest.mark.asyncio
    async def test_plan_checkpoint_sets_default_timeout_on_interaction(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="approved")

        result = await PlanCheckpointTool().execute(
            {"goal": "Refactor auth"},
            ToolContext(workspace=str(tmp_path), runtime=AgentToolRuntime(interaction=interact, events=PresentationToolUiEventPublisher())),
        )

        assert requests[0].timeout == 120.0

    def test_plan_checkpoint_description_mentions_document_first(self):
        assert (
            "design document" in PlanCheckpointTool.description
            or "Document first" in PlanCheckpointTool.description
        )
        
    @pytest.mark.asyncio
    async def test_plan_checkpoint_interaction_unavailable_has_summary(self, tmp_path):
        """interaction_unavailable path should have a non-empty summary."""
        result = await PlanCheckpointTool().execute(
            {"goal": "Edit files"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["plan_decision"] == "interaction_unavailable"
        assert result.summary is not None
        assert len(result.summary) > 0
        assert "unavailable" in result.summary

    @pytest.mark.asyncio
    async def test_plan_checkpoint_invalid_arguments_has_summary(self, tmp_path):
        """Invalid arguments path should have a non-empty summary."""
        result = await PlanCheckpointTool().execute(
            {"goal": 123},  # invalid type
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata.get("error") is True
        assert result.summary is not None
        assert len(result.summary) > 0
        assert "invalid" in result.summary

