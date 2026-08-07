from tests.tool_registry import build_registry
import json

import pytest

from voidx.agent.domain.task.state import ToolStatePatch
from tests.agent_tool_context import agent_tool_context as ToolContext
from voidx.tooling.application.registry import ToolRegistry
from voidx.agent.domain.automation.workflow import WorkflowRunState, WorkflowRunStatus


@pytest.mark.asyncio
async def test_advance_auto_selects_only_matching_active_workflow(tmp_path):
    ctx = ToolContext(
        workspace=str(tmp_path),
        workflow_runs=[
            WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
            WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    result = await build_registry().execute_tool(
        "workflow",
        {"action": "advance", "condition": "implemented", "goal": "验证实现"},
        ctx,
    )

    payload = json.loads(result.output)
    assert payload["from"] == "tdd"
    assert payload["condition"] == "implemented"


@pytest.mark.asyncio
async def test_terminal_condition_is_rejected_for_advance(tmp_path):
    ctx = ToolContext(
        workspace=str(tmp_path),
        workflow_runs=[WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE)],
    )

    result = await build_registry().execute_tool(
        "workflow",
        {"action": "advance", "condition": "terminal"},
        ctx,
    )

    payload = json.loads(result.output)
    assert result.metadata["workflow_guidance"]["reason"] == "invalid_exit"
    assert payload["applied"] is False


@pytest.mark.asyncio
async def test_advance_patch_matches_transition_payload(tmp_path):
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

    result = await build_registry().execute_tool(
        "workflow",
        {"action": "advance", "condition": "implemented", "goal": "验证实现"},
        ctx,
    )

    payload = json.loads(result.output)
    patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
    by_name = {run.name: run for run in patch.workflow_runs}
    assert by_name[payload["from"]].status == WorkflowRunStatus.SATISFIED
    assert all(by_name[name].status == WorkflowRunStatus.ACTIVE for name in payload["activated"])


@pytest.mark.asyncio
async def test_repeated_enter_error_does_not_change_workflow_state(tmp_path):
    runs = [WorkflowRunState(name="feedback", status=WorkflowRunStatus.ACTIVE, goal="处理反馈")]
    ctx = ToolContext(workspace=str(tmp_path), workflow_runs=runs)
    registry = build_registry()
    args = {"action": "enter", "workflow": "feedback", "goal": "处理反馈"}

    await registry.execute_tool("workflow", args, ctx)
    await registry.execute_tool("workflow", args, ctx)
    result = await registry.execute_tool("workflow", args, ctx)

    assert result.metadata["error"] is True
    assert "state_patch" not in result.metadata
    assert ctx.runtime.workflow_runs == runs
