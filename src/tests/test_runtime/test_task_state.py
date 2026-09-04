import sys
from pathlib import Path


from voidx.agent.domain.task.state import (

    GoalResolution,
    GoalSpec,
    PlanResolution,
    TaskState,
    TurnExchange,
    WorkflowContextMode,
)
from voidx.agent.domain.automation.workflow import WorkflowRoute

from voidx.agent.domain.automation.workflow import WorkflowRunState, WorkflowRunStatus


def _resolution(
    *,
    goal: GoalSpec | None = None,
    plan: PlanResolution | None = None,
) -> GoalResolution:
    return GoalResolution(
        goal=goal,
        plan=plan,
    )


def test_update_after_turn_records_goal_and_route():
    state = TaskState(recent_exchanges=[TurnExchange(user_text="之前", assistant_text="已处理")])
    goal = GoalSpec(desc="review diff")
    resolution = _resolution(
        goal=goal,
        plan=PlanResolution(join="review", leave="review"),
    )

    state.update_after_turn(resolution)

    assert state.current_goal == goal
    assert state.workflow_route == WorkflowRoute(join="review", leave="review")
    assert state.recent_exchanges[-1] == TurnExchange(user_text="之前", assistant_text="已处理")


def test_turn_without_goal_keeps_active_workflow_running():
    state = TaskState(
        current_goal=GoalSpec(desc="build feature"),
        workflow_route=WorkflowRoute(join="tdd", leave="verify"),
        workflow_runs={
            "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
        },
    )

    state.update_after_turn(_resolution())

    assert state.current_goal is not None
    assert state.workflow_context_mode == WorkflowContextMode.ACTIVE
    assert state.workflow_route is None
    assert "tdd" in state.workflow_runs
    assert state.workflow_runs["tdd"].status == WorkflowRunStatus.ACTIVE


def test_turn_without_goal_clears_route_and_keeps_runs():
    state = TaskState(
        current_goal=GoalSpec(desc="build feature"),
        workflow_route=WorkflowRoute(join="tdd", leave="verify"),
        workflow_runs={
            "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.SATISFIED),
        },
    )

    state.update_after_turn(_resolution())

    assert state.current_goal is not None
    assert state.workflow_route is None
    assert state.workflow_runs["tdd"].status == WorkflowRunStatus.SATISFIED


def test_update_after_turn_clears_workflow_when_goal_changes():
    old_goal = GoalSpec(desc="runtime context design")
    new_goal = GoalSpec(desc="current diff")
    state = TaskState(
        current_goal=old_goal,
        workflow_route=WorkflowRoute(join="brainstorm", leave="design"),
        workflow_runs={
            "brainstorm": WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
        },
    )

    state.update_after_turn(
        _resolution(
            goal=new_goal,
            plan=PlanResolution(join="review", leave="review"),
        ),
    )

    assert state.current_goal == new_goal
    assert state.workflow_route == WorkflowRoute(join="review", leave="review")
    assert state.workflow_runs == {}


def test_update_after_turn_preserves_workflow_for_same_goal():
    goal = GoalSpec(desc="build feature")
    active = WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE)
    state = TaskState(
        current_goal=goal,
        workflow_route=WorkflowRoute(join="tdd", leave="verify"),
        workflow_runs={"tdd": active},
    )

    state.update_after_turn(
        _resolution(
            goal=GoalSpec(desc="build feature"),
            plan=PlanResolution(join="tdd", leave="verify"),
        ),
    )

    assert state.current_goal == goal
    assert state.workflow_route == WorkflowRoute(join="tdd", leave="verify")
    assert state.workflow_runs == {"tdd": active}


def test_turn_without_goal_keeps_existing_goal_but_clears_route():
    goal = GoalSpec(desc="build feature")
    state = TaskState(
        current_goal=goal,
        workflow_route=WorkflowRoute(join="brainstorm", leave="verify"),
    )

    state.update_after_turn(_resolution())

    assert state.current_goal == goal
    assert state.workflow_route is None




def test_legacy_paused_workflow_state_maps_to_none():
    state = TaskState.model_validate({"workflow_context_mode": "paused"})

    assert state.workflow_context_mode == WorkflowContextMode.NONE


def test_set_goal_from_string_sets_goal_and_resets_workflow_context():
    state = TaskState(
        current_goal=GoalSpec(desc="review diff"),
        workflow_route=WorkflowRoute(join="review", leave="review"),
        workflow_runs={
            "review": WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE),
        },
    )

    state.set_goal("修复 bug")

    assert state.current_goal is not None
    assert state.current_goal.desc == "修复 bug"
    assert state.workflow_route is None
    assert state.workflow_runs == {}


def test_set_goal_accepts_goal_spec_and_resets_workflow_context():
    state = TaskState(
        workflow_route=WorkflowRoute(join="review", leave="review"),
        workflow_runs={
            "review": WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE),
        },
    )
    goal = GoalSpec(desc="write release notes")

    state.set_goal(goal)

    assert state.current_goal == goal
    assert state.workflow_route is None
    assert state.workflow_runs == {}


def test_clear_goal_resets_goal_state():
    state = TaskState()
    state.set_goal("修复 UI")

    state.clear_goal()

    assert state.current_goal is None
    assert state.workflow_route is None
    assert state.workflow_runs == {}


def test_goal_spec_normalizes_and_truncates():
    value = "  修复   workflow\n\n goal   参数改造  " + "x" * 160

    goal = GoalSpec(desc=value)

    assert goal.desc == ("修复 workflow goal 参数改造 " + "x" * 160)[:120]
    assert len(goal.desc) == 120


def test_active_workflow_runs_are_visible_without_context_mode():
    state = TaskState.model_validate({
        "workflow_runs": {
            "debug": WorkflowRunState(
                name="debug",
                status=WorkflowRunStatus.ACTIVE,
            ).model_dump(mode="json"),
        },
    })

    assert state.workflow_context_mode == WorkflowContextMode.ACTIVE
    assert [run.name for run in state.visible_workflow_runs()] == ["debug"]
