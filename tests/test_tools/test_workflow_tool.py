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
from voidx.tools.clarify import ClarifyTool, ClarifyInput, _infer_state_patch
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



class TestWorkflowTool:
    @pytest.mark.asyncio
    async def test_workflow_advance_activates_matching_successor(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(
                    name="tdd",
                    status=WorkflowRunStatus.ACTIVE,
                    transition_to=["verify"],
                )
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {
                "action": "advance",
                "condition": "implemented",
                "evidence": "focused test passed",
            },
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        by_name = {run.name: run for run in patch.workflow_runs}

        assert payload["from"] == "tdd"
        assert payload["action"] == "advance"
        assert payload["activated"] == ["verify"]
        assert payload["next_hints"]
        assert by_name["tdd"].status == WorkflowRunStatus.SATISFIED
        assert by_name["tdd"].evidence[0].condition == "implemented"
        assert by_name["verify"].status == WorkflowRunStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_workflow_enter_canonicalizes_node_and_closes_existing_active_runs(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
                WorkflowRunState(
                    name="plan",
                    status=WorkflowRunStatus.ACTIVE,
                )
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {
                "action": "enter",
                "workflow": "Debug",
                "evidence": "Need root-cause analysis",
            },
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        by_name = {run.name: run for run in patch.workflow_runs}

        assert result.metadata.get("error") is not True
        assert payload["action"] == "enter"
        assert payload["workflow"] == "debug"
        assert payload["activated"] == ["debug"]
        assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED
        assert by_name["plan"].status == WorkflowRunStatus.SATISFIED
        assert by_name["debug"].status == WorkflowRunStatus.ACTIVE
        assert by_name["debug"].evidence[0].kind == WorkflowStateEventKind.ACTIVATED.value
        assert by_name["debug"].personas == ["explore"]

    @pytest.mark.asyncio
    async def test_workflow_enter_unknown_node_returns_guidance_not_error(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {
                "action": "enter",
                "workflow": "TDD Cycle",
            },
            ctx,
        )

        payload = json.loads(result.output)

        assert result.metadata.get("error") is not True
        assert "state_patch" not in result.metadata
        assert result.metadata["workflow_guidance"]["reason"] == "invalid_node"
        assert payload["applied"] is False
        assert payload["reason"] == "invalid_node"
        assert "debug" in payload["available_nodes"]

    @pytest.mark.asyncio
    async def test_workflow_advance_accepts_empty_evidence(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(
                    name="tdd",
                    status=WorkflowRunStatus.ACTIVE,
                )
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {
                "action": "advance",
                "condition": "implemented",
            },
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        by_name = {run.name: run for run in patch.workflow_runs}

        assert result.metadata.get("error") is not True
        assert payload["evidence"] == ""
        assert by_name["tdd"].status == WorkflowRunStatus.SATISFIED
        assert by_name["verify"].status == WorkflowRunStatus.ACTIVE

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("condition", "target"),
        [
            ("needs_design", "brainstorm"),
            ("needs_plan", "plan"),
        ],
    )
    async def test_workflow_routes_feedback_deferred_items(self, tmp_path, condition, target):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(
                    name="feedback",
                    status=WorkflowRunStatus.ACTIVE,
                )
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {
                "action": "advance",
                "condition": condition,
                "evidence": "actionable feedback implemented; remaining item deferred",
            },
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        by_name = {run.name: run for run in patch.workflow_runs}

        assert payload["from"] == "feedback"
        assert payload["activated"] == [target]
        assert by_name["feedback"].status == WorkflowRunStatus.SATISFIED
        assert by_name["feedback"].evidence[0].condition == condition
        assert by_name[target].status == WorkflowRunStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_workflow_advance_invalid_condition_returns_guidance_not_error(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(
                    name="tdd",
                    status=WorkflowRunStatus.ACTIVE,
                )
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "advance", "condition": "approved"},
            ctx,
        )

        payload = json.loads(result.output)
        assert result.metadata.get("error") is not True
        assert result.metadata["workflow_guidance"]["reason"] == "invalid_exit"
        assert payload["applied"] is False
        assert any("implemented -> verify" in item for item in payload["available_exits"])
        assert "state_patch" not in result.metadata

    @pytest.mark.asyncio
    async def test_workflow_advance_missing_condition_returns_guidance_not_error(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(
                    name="tdd",
                    status=WorkflowRunStatus.ACTIVE,
                )
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "advance"},
            ctx,
        )

        payload = json.loads(result.output)
        assert result.metadata.get("error") is not True
        assert payload["reason"] == "condition_required"
        assert any("implemented -> verify" in item for item in payload["available_exits"])
        assert "state_patch" not in result.metadata

    @pytest.mark.asyncio
    async def test_workflow_missing_action_returns_action_guidance_not_legacy_advance(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(
                    name="tdd",
                    status=WorkflowRunStatus.ACTIVE,
                )
            ],
        )
        result = await ToolRegistry().execute_tool("workflow", {}, ctx)

        payload = json.loads(result.output)
        assert result.metadata.get("error") is not True
        assert result.metadata["workflow_guidance"]["reason"] == "action_required"
        assert payload["reason"] == "action_required"
        assert payload["available_actions"] == ["enter", "advance", "done"]
        assert "state_patch" not in result.metadata

    @pytest.mark.asyncio
    async def test_workflow_invalid_action_returns_guidance_not_validation_error(self, tmp_path):
        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "start"},
            ToolContext(workspace=str(tmp_path)),
        )

        payload = json.loads(result.output)
        assert result.metadata.get("error") is not True
        assert result.metadata["workflow_guidance"]["reason"] == "invalid_action"
        assert payload["action"] == "start"
        assert payload["available_actions"] == ["enter", "advance", "done"]
        assert "state_patch" not in result.metadata

    @pytest.mark.asyncio
    async def test_workflow_done_satisfies_all_active_without_successor(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(
                    name="verify",
                    status=WorkflowRunStatus.ACTIVE,
                ),
                WorkflowRunState(
                    name="review",
                    status=WorkflowRunStatus.ACTIVE,
                ),
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {
                "action": "done",
                "evidence": "small change verified",
            },
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])

        by_name = {run.name: run for run in patch.workflow_runs}
        assert payload["action"] == "done"
        assert payload["from"] == ["verify", "review"]
        assert payload["activated"] == []
        assert by_name["verify"].status == WorkflowRunStatus.SATISFIED
        assert by_name["review"].status == WorkflowRunStatus.SATISFIED

    @pytest.mark.asyncio
    async def test_workflow_done_without_active_nodes_is_idempotent(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path), workflow_runs=[])
        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "done"},
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        assert result.metadata.get("error") is not True
        assert payload["no_active_nodes"] is True
        assert patch.workflow_runs == []

    @pytest.mark.asyncio
    async def test_workflow_advance_ambiguous_condition_returns_guidance(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
                WorkflowRunState(name="plan", status=WorkflowRunStatus.ACTIVE),
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "advance", "condition": "approved"},
            ctx,
        )

        payload = json.loads(result.output)
        assert result.metadata.get("error") is not True
        assert result.metadata["workflow_guidance"]["reason"] == "ambiguous_exit"
        assert payload["applied"] is False
        assert payload["candidates"] == ["brainstorm", "plan"]
        assert "state_patch" not in result.metadata

    @pytest.mark.asyncio
    async def test_workflow_advance_explicit_workflow_closes_other_active_runs(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
                WorkflowRunState(name="plan", status=WorkflowRunStatus.ACTIVE),
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {
                "action": "advance",
                "workflow": "plan",
                "condition": "approved",
                "evidence": "plan approved",
            },
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        by_name = {run.name: run for run in patch.workflow_runs}

        assert payload["from"] == "plan"
        assert payload["activated"] == ["tdd"]
        assert by_name["plan"].status == WorkflowRunStatus.SATISFIED
        assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED
        assert by_name["brainstorm"].evidence[-1].condition == "superseded_by_workflow_advance"
        assert by_name["tdd"].status == WorkflowRunStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_workflow_enter_already_active_is_idempotent(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE),
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "enter", "workflow": "debug"},
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])

        assert payload["already_active"] is True
        assert patch.workflow_runs[0].name == "debug"
        assert patch.workflow_runs[0].status == WorkflowRunStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_workflow_enter_already_active_normalizes_duplicate_runs(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE),
                WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE),
            ],
        )
        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "enter", "workflow": "debug"},
            ctx,
        )

        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        active = [run for run in patch.workflow_runs if run.status == WorkflowRunStatus.ACTIVE]

        assert len(active) == 1
        assert active[0].name == "debug"

    @pytest.mark.asyncio
    async def test_legacy_advance_workflow_tool_id_is_not_supported(self, tmp_path):
        result = await ToolRegistry().execute_tool(
            "advance_workflow",
            {"condition": "done"},
            ToolContext(workspace=str(tmp_path), workflow_runs=[]),
        )

        assert result.output.startswith("Unknown tool: advance_workflow.")


