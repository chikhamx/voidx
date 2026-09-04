"""Workflow context changes only through explicit state."""

from voidx.agent.application.runtime_context import RuntimeContextBuilder
from voidx.agent.domain.automation.workflow import (
    WorkflowRoute,
    WorkflowRunState,
    WorkflowRunStatus,
)
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
from voidx.agent.domain.task.state import GoalResolution, GoalSpec, TaskState, WorkflowContextMode
from voidx.config import Config


def _active_feedback() -> WorkflowRunState:
    return WorkflowRunState(
        name="feedback",
        status=WorkflowRunStatus.ACTIVE,
        personas=["review"],
        scope="Apply review feedback",
    )


def test_goal_changing_turn_clears_workflow_context(tmp_path):
    state = TaskState(
        current_goal=GoalSpec(desc="Apply review feedback"),
        workflow_route=WorkflowRoute(join="feedback", leave="tdd"),
        workflow_runs={"feedback": _active_feedback()},
    )

    state.update_after_turn(GoalResolution(goal=GoalSpec(desc="Explain the review process")))

    assert state.workflow_context_mode == WorkflowContextMode.NONE
    assert state.workflow_runs == {}
    assert state.workflow_route is None

    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="coordinate",
        interaction_mode="auto",
        workflow_dag=DEFAULT_WORKFLOW_DAG,
        workflow_runs=[],
        task_state=state,
    ).build()
    rendered = context.render_task_context()
    assert "Workflow context: none" in rendered
    assert "Background workflows:" not in rendered
    assert "Active workflows:" not in rendered
    assert "Workflow route:" not in rendered
    assert "Workflow transitions [feedback]" not in rendered


def test_legacy_task_state_with_active_runs_defaults_to_active():
    state = TaskState.model_validate({
        "current_goal": {"desc": "legacy"},
        "workflow_runs": {"feedback": _active_feedback().model_dump(mode="json")},
    })

    assert state.workflow_context_mode == WorkflowContextMode.ACTIVE


def test_task_state_without_workflow_defaults_to_none():
    assert TaskState().workflow_context_mode == WorkflowContextMode.NONE
