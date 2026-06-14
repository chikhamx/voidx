import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.runtime.task_state import GoalResolution, GoalType, PendingApproval, TaskState, goal_from_text
from voidx.workflow.reconcile import reconcile_workflow_runs_for_turn
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus


def _state_with_run(run: WorkflowRunState, *, pending: PendingApproval | None = None) -> TaskState:
    return TaskState(
        current_goal=goal_from_text("agent_name 语义清理", goal_type=GoalType.DESIGN),
        pending_approval=pending,
        workflow_runs={run.name: run},
    )


def test_reconcile_advances_brainstorm_to_design_doc_when_user_approves_spec():
    run = WorkflowRunState(
        name="brainstorm",
        status=WorkflowRunStatus.ACTIVE,
        goal_type="design",
        scope="agent_name 语义清理",
    )
    state = _state_with_run(
        run,
        pending=PendingApproval(scope="agent_name 语义清理", source_goal_type=GoalType.DESIGN),
    )
    resolution = GoalResolution(
        goal=goal_from_text("写一个 spec", goal_type=GoalType.DOC, user_requested_write=True),
        next_workflow="design-doc",
    )
    state.update_after_turn(resolution, "可以，先写一个 spec")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=7,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert by_name["brainstorm"].evidence[-1].ref == "auto:turn_reconcile:brainstorm_to_design-doc"
    assert by_name["brainstorm"].evidence[-1].condition == "approved"
    assert by_name["design-doc"].status == WorkflowRunStatus.ACTIVE
    assert by_name["design-doc"].reason == "transition from brainstorm via approved"
    assert by_name["design-doc"].activated_turn == 7


def test_reconcile_does_not_advance_plain_approval_without_doc_request():
    run = WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE)
    state = _state_with_run(
        run,
        pending=PendingApproval(scope="agent_name 语义清理", source_goal_type=GoalType.DESIGN),
    )
    resolution = GoalResolution(
        goal=goal_from_text("agent_name 语义清理", goal_type=GoalType.FEATURE, user_requested_write=True),
    )
    state.update_after_turn(resolution, "可以")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["brainstorm"].status == WorkflowRunStatus.ACTIVE
    assert "design-doc" not in by_name


def test_reconcile_preserves_unrelated_active_workflow_runs():
    verify = WorkflowRunState(
        name="verify",
        status=WorkflowRunStatus.ACTIVE,
        reason="transition from tdd via implemented",
    )
    before = TaskState(workflow_runs={"verify": verify})
    after = before.model_copy(deep=True)
    after.current_goal = goal_from_text("写一个 spec", goal_type=GoalType.DOC, user_requested_write=True)
    resolution = GoalResolution(goal=after.current_goal)

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=after,
    )

    assert updated == [verify]


def test_reconcile_advances_brainstorm_to_plan_when_next_workflow_requests_plan():
    run = WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE)
    state = _state_with_run(run)
    resolution = GoalResolution(next_workflow="plan")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=3,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert by_name["brainstorm"].evidence[-1].condition == "skip_to_plan"
    assert by_name["plan"].status == WorkflowRunStatus.ACTIVE
    assert by_name["plan"].activated_turn == 3


def test_reconcile_does_not_satisfy_source_when_target_is_already_active():
    brainstorm = WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE)
    design_doc = WorkflowRunState(name="design-doc", status=WorkflowRunStatus.ACTIVE)
    state = TaskState(
        workflow_runs={
            "brainstorm": brainstorm,
            "design-doc": design_doc,
        },
    )
    resolution = GoalResolution(next_workflow="design-doc")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["brainstorm"].status == WorkflowRunStatus.ACTIVE
    assert by_name["design-doc"].status == WorkflowRunStatus.ACTIVE
    assert by_name["brainstorm"].evidence == []


def test_reconcile_ignores_unknown_next_workflow():
    run = WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE)
    state = _state_with_run(run)
    resolution = GoalResolution(next_workflow="nonexistent")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
    )

    assert updated == [run]


def test_reconcile_advances_verify_to_review_when_next_workflow_requests_review():
    verify = WorkflowRunState(
        name="verify",
        status=WorkflowRunStatus.ACTIVE,
        reason="transition from tdd via implemented",
    )
    state = TaskState(workflow_runs={"verify": verify})
    resolution = GoalResolution(next_workflow="review")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=5,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["verify"].status == WorkflowRunStatus.SATISFIED
    assert by_name["verify"].evidence[-1].condition == "passed_substantial"
    assert by_name["review"].status == WorkflowRunStatus.ACTIVE
    assert by_name["review"].activated_turn == 5


def test_reconcile_advances_feedback_to_brainstorm_when_next_workflow_requests_brainstorm():
    feedback = WorkflowRunState(
        name="feedback",
        status=WorkflowRunStatus.ACTIVE,
        goal_type="review",
        scope="review feedback",
    )
    state = TaskState(workflow_runs={"feedback": feedback})
    resolution = GoalResolution(next_workflow="brainstorm")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=8,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["feedback"].status == WorkflowRunStatus.SATISFIED
    assert by_name["feedback"].evidence[-1].condition == "needs_design"
    assert by_name["brainstorm"].status == WorkflowRunStatus.ACTIVE
    assert by_name["brainstorm"].reason == "transition from feedback via needs_design"
    assert by_name["brainstorm"].activated_turn == 8


def test_reconcile_advances_feedback_to_plan_when_next_workflow_requests_plan_from_feedback():
    feedback = WorkflowRunState(
        name="feedback",
        status=WorkflowRunStatus.ACTIVE,
        goal_type="review",
        scope="review feedback",
    )
    state = TaskState(workflow_runs={"feedback": feedback})
    resolution = GoalResolution(next_workflow="plan")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=9,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["feedback"].status == WorkflowRunStatus.SATISFIED
    assert by_name["feedback"].evidence[-1].condition == "needs_plan"
    assert by_name["plan"].status == WorkflowRunStatus.ACTIVE
    assert by_name["plan"].reason == "transition from feedback via needs_plan"
    assert by_name["plan"].activated_turn == 9


def test_reconcile_picks_first_source_when_multiple_active_nodes_target_same_workflow():
    debug = WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE)
    tdd = WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE)
    state = TaskState(workflow_runs={"debug": debug, "tdd": tdd})
    resolution = GoalResolution(next_workflow="verify")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=4,
    )

    by_name = {item.name: item for item in updated}
    assert "verify" in by_name
    assert by_name["verify"].status == WorkflowRunStatus.ACTIVE
    satisfied_sources = [
        name for name, run in by_name.items()
        if run.status == WorkflowRunStatus.SATISFIED
    ]
    assert len(satisfied_sources) == 1
    assert satisfied_sources[0] == "debug"
