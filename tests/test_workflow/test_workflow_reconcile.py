from voidx.runtime.intent import TaskIntent
from voidx.runtime.task_state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
)
from voidx.workflow.reconcile import reconcile_workflow_runs_for_turn
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus


def _goal(desc: str = "agent_name semantic cleanup") -> GoalSpec:
    return GoalSpec(desc=desc)


def _resolution(
    *,
    goal: GoalSpec | None = None,
    join: str | None = None,
    leave: str | None = None,
) -> GoalResolution:
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
        goal=goal,
        plan=PlanResolution(join=join, leave=leave) if join is not None else None,
    )


def _state_with_run(run: WorkflowRunState, *, goal: GoalSpec | None = None) -> TaskState:
    return TaskState(
        current_goal=goal or _goal(),
        workflow_runs={run.name: run},
    )


def test_reconcile_advances_brainstorm_to_design_when_join_requests_design():
    run = WorkflowRunState(
        name="brainstorm",
        status=WorkflowRunStatus.ACTIVE,
        goal_type="design",
        scope="agent_name semantic cleanup",
    )
    goal = _goal("write a spec")
    state = _state_with_run(run, goal=goal)
    resolution = _resolution(goal=goal, join="design", leave="design")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=7,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert by_name["brainstorm"].evidence[-1].ref == "auto:turn_reconcile:brainstorm_to_design"
    assert by_name["brainstorm"].evidence[-1].condition == "approved"
    assert by_name["design"].status == WorkflowRunStatus.ACTIVE
    assert by_name["design"].reason == "transition from brainstorm via approved"
    assert by_name["design"].activated_turn == 7


def test_reconcile_does_not_advance_without_plan_join():
    run = WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE)
    state = _state_with_run(run)
    resolution = _resolution(goal=_goal("keep discussing"))

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
    )

    assert updated == [run]


def test_reconcile_preserves_unrelated_active_workflow_runs():
    verify = WorkflowRunState(
        name="verify",
        status=WorkflowRunStatus.ACTIVE,
        reason="transition from tdd via implemented",
    )
    state = TaskState(
        current_goal=_goal("write a spec"),
        workflow_runs={"verify": verify},
    )
    resolution = _resolution(goal=state.current_goal)

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
    )

    assert updated == [verify]


def test_reconcile_activates_plan_join_when_no_workflow_is_active():
    goal = _goal("review current diff")
    state = TaskState(current_goal=goal)
    resolution = _resolution(goal=goal, join="review", leave="review")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=2,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["review"].status == WorkflowRunStatus.ACTIVE
    assert by_name["review"].reason == "resolver plan.join"
    assert by_name["review"].goal_type == "review"
    assert by_name["review"].activated_turn == 2


def test_reconcile_clears_completed_workflow_runs_when_next_turn_has_no_join():
    verify = WorkflowRunState(
        name="verify",
        status=WorkflowRunStatus.SATISFIED,
        reason="transition from tdd via implemented",
    )
    state = TaskState(workflow_runs={"verify": verify})
    resolution = _resolution(goal=_goal("prepare push"))

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=12,
    )

    assert updated == []


def test_reconcile_advances_brainstorm_to_plan_when_join_requests_plan():
    run = WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE)
    state = _state_with_run(run)
    resolution = _resolution(join="plan")

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


def test_reconcile_supersedes_brainstorm_when_join_requests_tdd():
    run = WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE)
    goal = _goal("start implementation")
    state = _state_with_run(run, goal=goal)
    resolution = _resolution(goal=goal, join="tdd", leave="verify")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=11,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert by_name["brainstorm"].evidence[-1].condition == "superseded_by_intent"
    assert by_name["tdd"].status == WorkflowRunStatus.ACTIVE
    assert by_name["tdd"].reason == "intent override from brainstorm"
    assert by_name["tdd"].activated_turn == 11


def test_reconcile_keeps_debug_dag_transition_when_join_requests_tdd():
    debug = WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE)
    goal = _goal("implement bug fix")
    state = TaskState(current_goal=goal, workflow_runs={"debug": debug})
    resolution = _resolution(goal=goal, join="tdd")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["debug"].status == WorkflowRunStatus.SATISFIED
    assert by_name["debug"].evidence[-1].condition == "nontrivial_fix"
    assert by_name["tdd"].status == WorkflowRunStatus.ACTIVE


def test_reconcile_satisfies_other_active_runs_when_target_is_already_active():
    brainstorm = WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE)
    design = WorkflowRunState(name="design", status=WorkflowRunStatus.ACTIVE)
    state = TaskState(
        workflow_runs={
            "brainstorm": brainstorm,
            "design": design,
        },
    )
    resolution = _resolution(join="design")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
    )

    by_name = {item.name: item for item in updated}
    assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert by_name["brainstorm"].evidence[-1].condition == "superseded_by_active_target"
    assert by_name["design"].status == WorkflowRunStatus.ACTIVE


def test_reconcile_keeps_single_active_target_when_no_other_active_runs():
    design = WorkflowRunState(name="design", status=WorkflowRunStatus.ACTIVE)
    state = TaskState(workflow_runs={"design": design})
    resolution = _resolution(join="design")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
    )

    assert updated == [design]


def test_reconcile_preserves_active_workflow_without_join_for_same_goal():
    tdd = WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE)
    goal = _goal("build feature")
    state = TaskState(current_goal=goal, workflow_runs={"tdd": tdd})
    resolution = _resolution(goal=goal)

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
    )

    assert updated == [tdd]


def test_reconcile_ignores_unknown_plan_join():
    run = WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE)
    state = _state_with_run(run)
    resolution = _resolution(join="nonexistent")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
    )

    assert updated == [run]


def test_reconcile_advances_verify_to_review_when_dag_edge_exists():
    verify = WorkflowRunState(
        name="verify",
        status=WorkflowRunStatus.ACTIVE,
        reason="transition from tdd via implemented",
    )
    state = TaskState(workflow_runs={"verify": verify})
    resolution = _resolution(join="review")

    updated = reconcile_workflow_runs_for_turn(
        goal_resolution=resolution,
        after_state=state,
        turn_count=5,
    )

    assert len(updated) == 2
    by_name = {r.name: r for r in updated}
    assert by_name["verify"].status == WorkflowRunStatus.SATISFIED
    assert by_name["review"].status == WorkflowRunStatus.ACTIVE


def test_reconcile_advances_feedback_to_brainstorm_when_join_requests_brainstorm():
    feedback = WorkflowRunState(
        name="feedback",
        status=WorkflowRunStatus.ACTIVE,
        goal_type="review",
        scope="review feedback",
    )
    state = TaskState(workflow_runs={"feedback": feedback})
    resolution = _resolution(join="brainstorm")

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


def test_reconcile_advances_feedback_to_plan_when_join_requests_plan_from_feedback():
    feedback = WorkflowRunState(
        name="feedback",
        status=WorkflowRunStatus.ACTIVE,
        goal_type="review",
        scope="review feedback",
    )
    state = TaskState(workflow_runs={"feedback": feedback})
    resolution = _resolution(join="plan")

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
    resolution = _resolution(join="verify")

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
