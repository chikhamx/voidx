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
from voidx.tools.document import LoadDocTemplateTool, LoadDocTemplateInput
from voidx.tools.checkpoint import PlanCheckpointTool
from voidx.agent.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


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
                "goal": "验证实现结果",
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
                "goal": "分析根因",
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
                    goal="实现功能",
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
        assert payload["goal"] == "实现功能"
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
                "goal": "处理反馈后续事项",
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
    async def test_workflow_advance_invalid_active_workflow_reports_current_node(self, tmp_path):
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
            {"action": "advance", "workflow": "debug", "condition": "implemented"},
            ctx,
        )

        payload = json.loads(result.output)
        assert result.metadata.get("error") is not True
        assert result.metadata["workflow_guidance"]["reason"] == "invalid_active_workflow"
        assert payload["applied"] is False
        assert payload["current_node"] == "tdd"
        assert "Current node: tdd" in payload["guidance"]
        assert "active_nodes" not in payload
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
            {"action": "done"},
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
                "goal": "实现获批计划",
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
            {"action": "enter", "workflow": "debug", "goal": "调试当前问题"},
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
            {"action": "enter", "workflow": "debug", "goal": "调试当前问题"},
            ctx,
        )

        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        active = [run for run in patch.workflow_runs if run.status == WorkflowRunStatus.ACTIVE]

        assert len(active) == 1
        assert active[0].name == "debug"

    @pytest.mark.asyncio
    async def test_workflow_enter_sets_goal_state_patch_and_run_goal(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path), workflow_runs=[])

        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "enter", "workflow": "debug", "goal": "修复登录 bug"},
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        by_name = {run.name: run for run in patch.workflow_runs}

        assert payload["goal"] == "修复登录 bug"
        assert patch.goal is not None
        assert patch.goal.desc == "修复登录 bug"
        assert by_name["debug"].goal == "修复登录 bug"
        assert by_name["debug"].evidence[0].summary == "修复登录 bug"

    @pytest.mark.asyncio
    async def test_workflow_enter_requires_goal(self, tmp_path):
        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "enter", "workflow": "debug"},
            ToolContext(workspace=str(tmp_path)),
        )

        payload = json.loads(result.output)
        assert result.metadata.get("error") is not True
        assert result.metadata["workflow_guidance"]["reason"] == "goal_required"
        assert payload["applied"] is False
        assert "state_patch" not in result.metadata

    @pytest.mark.asyncio
    async def test_workflow_enter_already_active_updates_goal(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE, goal="旧目标"),
            ],
        )

        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "enter", "workflow": "debug", "goal": "修复新问题"},
            ctx,
        )

        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        assert patch.goal is not None
        assert patch.goal.desc == "修复新问题"
        assert patch.workflow_runs[0].goal == "修复新问题"

    @pytest.mark.asyncio
    async def test_workflow_advance_inherits_goal_without_input(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(
                    name="tdd",
                    status=WorkflowRunStatus.ACTIVE,
                    goal="实现 workflow goal 参数改造",
                    transition_to=["verify"],
                )
            ],
        )

        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "advance", "condition": "implemented"},
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        by_name = {run.name: run for run in patch.workflow_runs}

        assert payload["goal"] == "实现 workflow goal 参数改造"
        assert payload["goal_source"] == "run"
        assert patch.goal is not None
        assert patch.goal.desc == "实现 workflow goal 参数改造"
        assert by_name["tdd"].goal == "实现 workflow goal 参数改造"
        assert by_name["verify"].goal == "实现 workflow goal 参数改造"

    @pytest.mark.asyncio
    async def test_workflow_advance_retargets_successor_only(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(
                    name="tdd",
                    status=WorkflowRunStatus.ACTIVE,
                    goal="旧实现目标",
                    transition_to=["verify"],
                )
            ],
        )

        result = await ToolRegistry().execute_tool(
            "workflow",
            {
                "action": "advance",
                "condition": "implemented",
                "goal": "验证 workflow goal 改造",
            },
            ctx,
        )

        payload = json.loads(result.output)
        patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
        by_name = {run.name: run for run in patch.workflow_runs}

        assert payload["goal"] == "验证 workflow goal 改造"
        assert payload["goal_source"] == "input"
        assert by_name["tdd"].goal == "旧实现目标"
        assert by_name["verify"].goal == "验证 workflow goal 改造"

    @pytest.mark.asyncio
    async def test_workflow_advance_requires_goal_when_no_context(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE)],
        )

        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "advance", "condition": "implemented"},
            ctx,
        )

        payload = json.loads(result.output)
        assert result.metadata.get("error") is not True
        assert result.metadata["workflow_guidance"]["reason"] == "goal_required"
        assert payload["applied"] is False
        assert "state_patch" not in result.metadata

    @pytest.mark.asyncio
    async def test_workflow_advance_goal_required_guidance_tracks_repeats(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE)],
        )
        reg = ToolRegistry()

        r1 = await reg.execute_tool("workflow", {"action": "advance", "condition": "implemented"}, ctx)
        p1 = json.loads(r1.output)
        assert p1["reason"] == "goal_required"
        assert "repeat_warning" not in p1
        assert r1.metadata.get("error") is not True

        r2 = await reg.execute_tool("workflow", {"action": "advance", "condition": "implemented"}, ctx)
        p2 = json.loads(r2.output)
        assert p2["reason"] == "goal_required"
        assert "repeat_warning" in p2
        assert r2.metadata.get("error") is not True

        r3 = await reg.execute_tool("workflow", {"action": "advance", "condition": "implemented"}, ctx)
        p3 = json.loads(r3.output)
        assert p3["reason"] == "goal_required"
        assert "repeat_warning" in p3
        assert r3.metadata.get("error") is True
        assert r3.metadata["reason"] == "repeated_workflow_advance"

    @pytest.mark.asyncio
    async def test_workflow_done_does_not_emit_goal_patch(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[WorkflowRunState(name="verify", status=WorkflowRunStatus.ACTIVE, goal="验证改造")],
        )

        result = await ToolRegistry().execute_tool(
            "workflow",
            {"action": "done", "goal": "应被忽略"},
            ctx,
        )

        payload = json.loads(result.output)
        raw_patch = result.metadata["state_patch"]
        patch = ToolStatePatch.model_validate(raw_patch)

        assert "goal" not in payload
        assert "goal" not in raw_patch
        assert patch.goal is None
    @pytest.mark.asyncio
    async def test_legacy_advance_workflow_tool_id_is_not_supported(self, tmp_path):
        result = await ToolRegistry().execute_tool(
            "advance_workflow",
            {"condition": "done"},
            ToolContext(workspace=str(tmp_path), workflow_runs=[]),
        )

        assert result.output.startswith("Unknown tool: advance_workflow.")



    @pytest.mark.asyncio
    async def test_repeated_enter_returns_warning_then_error(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="feedback", status=WorkflowRunStatus.ACTIVE),
            ],
        )
        reg = ToolRegistry()

        r1 = await reg.execute_tool("workflow", {"action": "enter", "workflow": "feedback", "goal": "处理反馈"}, ctx)
        p1 = json.loads(r1.output)
        assert p1["already_active"] is True
        assert "repeat_warning" not in p1
        assert r1.metadata.get("error") is not True

        r2 = await reg.execute_tool("workflow", {"action": "enter", "workflow": "feedback", "goal": "处理反馈"}, ctx)
        p2 = json.loads(r2.output)
        assert p2["already_active"] is True
        assert "repeat_warning" in p2
        assert r2.metadata.get("error") is not True

        r3 = await reg.execute_tool("workflow", {"action": "enter", "workflow": "feedback", "goal": "处理反馈"}, ctx)
        p3 = json.loads(r3.output)
        assert p3["already_active"] is True
        assert "repeat_warning" in p3
        assert r3.metadata.get("error") is True
        assert r3.metadata["reason"] == "repeated_workflow_enter"

    @pytest.mark.asyncio
    async def test_repeated_advance_returns_warning_then_error(self, tmp_path):
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE, goal="验证实现", transition_to=["verify"]),
            ],
        )
        reg = ToolRegistry()

        r1 = await reg.execute_tool(
            "workflow", {"action": "advance", "condition": "implemented", "goal": "验证实现"}, ctx
        )
        p1 = json.loads(r1.output)
        assert p1["from"] == "tdd"
        assert "repeat_warning" not in p1

        # Re-activate tdd to simulate the LLM advancing the same node again
        ctx2 = ToolContext(
            workspace=str(tmp_path),
            workflow_repeat_tracker=ctx._workflow_repeat_tracker,
            workflow_runs=[
                WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE, goal="验证实现", transition_to=["verify"]),
            ],
        )
        r2 = await reg.execute_tool(
            "workflow", {"action": "advance", "condition": "implemented", "goal": "验证实现"}, ctx2
        )
        p2 = json.loads(r2.output)
        assert "repeat_warning" in p2
        assert r2.metadata.get("error") is not True

        ctx3 = ToolContext(
            workspace=str(tmp_path),
            workflow_repeat_tracker=ctx._workflow_repeat_tracker,
            workflow_runs=[
                WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE, goal="验证实现", transition_to=["verify"]),
            ],
        )
        r3 = await reg.execute_tool(
            "workflow", {"action": "advance", "condition": "implemented", "goal": "验证实现"}, ctx3
        )
        p3 = json.loads(r3.output)
        assert "repeat_warning" in p3
        assert r3.metadata.get("error") is True
        assert r3.metadata["reason"] == "repeated_workflow_advance"

    @pytest.mark.asyncio
    async def test_repeated_advance_after_satisfied_triggers_warning_via_guidance(self, tmp_path):
        """In real usage, advance succeeds → node satisfied → 2nd advance hits guidance path.
        The guidance path must also detect repeats."""
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE, goal="验证实现", transition_to=["verify"]),
            ],
        )
        reg = ToolRegistry()

        # 1st advance: succeeds, tdd → satisfied, verify activated
        r1 = await reg.execute_tool(
            "workflow", {"action": "advance", "condition": "implemented", "goal": "验证实现"}, ctx
        )
        p1 = json.loads(r1.output)
        assert p1["from"] == "tdd"
        assert "repeat_warning" not in p1

        # 2nd advance same condition: tdd is now satisfied, no active node matches
        # → guidance path (no_active_nodes or invalid_exit), should get repeat_warning
        r2 = await reg.execute_tool(
            "workflow", {"action": "advance", "condition": "implemented", "goal": "验证实现"}, ctx
        )
        p2 = json.loads(r2.output)
        assert "repeat_warning" in p2
        assert r2.metadata.get("error") is not True

        # 3rd: should return error
        r3 = await reg.execute_tool(
            "workflow", {"action": "advance", "condition": "implemented", "goal": "验证实现"}, ctx
        )
        p3 = json.loads(r3.output)
        assert "repeat_warning" in p3
        assert r3.metadata.get("error") is True
        assert r3.metadata["reason"] == "repeated_workflow_advance"

    @pytest.mark.asyncio
    async def test_repeated_advance_guidance_text_contains_transition_succeeded(self, tmp_path):
        """Advance repeat guidance should say transition succeeded, not 'already active'."""
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE, goal="验证实现", transition_to=["verify"]),
            ],
        )
        reg = ToolRegistry()

        r1 = await reg.execute_tool(
            "workflow", {"action": "advance", "condition": "implemented", "goal": "验证实现"}, ctx
        )

        # Re-activate tdd to simulate 2nd advance call
        ctx2 = ToolContext(
            workspace=str(tmp_path),
            workflow_repeat_tracker=ctx._workflow_repeat_tracker,
            workflow_runs=[
                WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE, goal="验证实现", transition_to=["verify"]),
            ],
        )
        r2 = await reg.execute_tool(
            "workflow", {"action": "advance", "condition": "implemented", "goal": "验证实现"}, ctx2
        )
        p2 = json.loads(r2.output)
        guidance = p2["repeat_warning"]
        assert "already advanced" in guidance or "transition succeeded" in guidance, (
            f"Advance guidance should mention transition completion, got: {guidance}"
        )
        assert "already active" not in guidance, (
            f"Advance guidance should NOT say 'already active', got: {guidance}"
        )

    @pytest.mark.asyncio
    async def test_repeated_enter_guidance_text_contains_already_active(self, tmp_path):
        """Enter repeat guidance should say 'already active'."""
        ctx = ToolContext(
            workspace=str(tmp_path),
            workflow_runs=[
                WorkflowRunState(name="feedback", status=WorkflowRunStatus.ACTIVE),
            ],
        )
        reg = ToolRegistry()

        r1 = await reg.execute_tool("workflow", {"action": "enter", "workflow": "feedback", "goal": "处理反馈"}, ctx)

        r2 = await reg.execute_tool("workflow", {"action": "enter", "workflow": "feedback", "goal": "处理反馈"}, ctx)
        p2 = json.loads(r2.output)
        guidance = p2["repeat_warning"]
        assert "already active" in guidance, (
            f"Enter guidance should say 'already active', got: {guidance}"
        )
