import pytest

from voidx.agent.application.instruction import InstructionService
from voidx.agent.domain.task.intent import TaskIntent
from voidx.agent.domain.task.state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
    ToolStatePatch,
)
from voidx.agent.adapters.tools.automation.workflow import WorkflowTool
from tests.agent_tool_context import agent_tool_context as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.agent.application.automation.workflow.reconcile import reconcile_workflow_runs_for_turn
from voidx.agent.application.automation.workflow.runtime import advance_workflow_states
from voidx.agent.domain.automation.workflow import (
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
)


def test_goal_type_compatibility_exports_are_removed():
    import voidx.agent.domain.task.state as agent_task_state
    import voidx.runtime as runtime

    for module in (runtime, agent_task_state):
        assert not hasattr(module, "GoalType")
        assert not hasattr(module, "infer_goal_type")
        assert not hasattr(module, "goal_type_value")


def test_state_update_applies_intent_resolution_patch():
    from voidx.agent.adapters.langgraph.runtime.tool_executor import _ExecutedTool, _state_update_from_executed_tools

    patch = {
        "intent": {
            "type": "general",
            "desc": "tool clarified this is general conversation",
        }
    }
    executed = [
        _ExecutedTool(
            message=None,
            result=ToolResult(output="clarified", metadata={"state_patch": patch}),
            tool_call={"name": "clarify"},
        )
    ]

    update = _state_update_from_executed_tools(executed)

    assert update["task_intent"] == "general"


@pytest.mark.asyncio
async def test_workflow_context_skips_trigger_matching_when_resolver_join_is_absent(tmp_path):
    context = await InstructionService(str(tmp_path)).workflow_context_for(
        goal_type="feature",
        workflow_start=None,
    )

    assert context.active == []
    assert context.runs == []


def test_reconcile_uses_plan_join_as_route_target():
    goal = GoalSpec(desc="review current diff")
    resolution = GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
        goal=goal,
        plan=PlanResolution(join="review", leave="review"),
    )
    state = TaskState(current_goal=goal)

    runs = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=3,
    )

    assert len(runs) == 1
    assert runs[0].name == "review"
    assert runs[0].status == WorkflowRunStatus.ACTIVE
    assert runs[0].reason == "resolver plan.join"


def test_terminal_done_cascade_skips_active_downstream_nodes():
    runs = [
        WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
        WorkflowRunState(name="plan", status=WorkflowRunStatus.ACTIVE),
    ]
    event = WorkflowStateEvent(
        workflow="brainstorm",
        kind=WorkflowStateEventKind.SATISFIED,
        ref="test:done",
        ok=True,
        summary="Design is no longer needed.",
        reason="user moved on",
        condition="done",
    )

    updated = advance_workflow_states(runs, [event], turn_count=5)
    by_name = {run.name: run for run in updated}

    assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert by_name["plan"].status == WorkflowRunStatus.SKIPPED
    assert by_name["plan"].evidence[-1].condition == "done"


@pytest.mark.asyncio
async def test_workflow_done_no_active_node_returns_guidance_success_not_error(tmp_path):
    result = await WorkflowTool().execute(
        {"action": "done"},
        ToolContext(workspace=str(tmp_path), workflow_runs=[]),
    )

    assert result.metadata.get("error") is not True
    patch = ToolStatePatch.model_validate(result.metadata["state_patch"])
    assert patch.workflow_runs == []
