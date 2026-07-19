import sys
from pathlib import Path


from voidx.runtime.task_state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
    TurnExchange,
    WorkflowRoute,
)
from voidx.runtime.intent import TaskIntent
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus


def _resolution(
    *,
    intent: TaskIntent = TaskIntent.CODING,
    goal: GoalSpec | None = None,
    plan: PlanResolution | None = None,
) -> GoalResolution:
    return GoalResolution(
        intent=IntentResolution(type=intent),
        goal=goal,
        plan=plan,
    )


def test_update_after_turn_records_intent_goal_and_route():
    state = TaskState(recent_exchanges=[TurnExchange(user_text="之前", assistant_text="已处理")])
    goal = GoalSpec(desc="review diff")
    resolution = _resolution(
        goal=goal,
        plan=PlanResolution(join="review", leave="review"),
    )

    state.update_after_turn(resolution, "review this")

    assert state.previous_intent == TaskIntent.CODING
    assert state.current_intent == TaskIntent.CODING
    assert state.current_goal == goal
    assert state.workflow_route == WorkflowRoute(join="review", leave="review")
    assert state.recent_exchanges[-1] == TurnExchange(user_text="之前", assistant_text="已处理")


def test_general_turn_preserves_active_workflow():
    state = TaskState(
        current_goal=GoalSpec(desc="build feature"),
        workflow_route=WorkflowRoute(join="tdd", leave="verify"),
        workflow_runs={
            "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
        },
    )

    state.update_after_turn(_resolution(intent=TaskIntent.GENERAL), "thanks")

    assert state.current_intent == TaskIntent.GENERAL
    assert state.current_goal is not None
    assert state.workflow_route is not None
    assert "tdd" in state.workflow_runs


def test_general_turn_keeps_goal_when_no_active_workflow():
    state = TaskState(
        current_goal=GoalSpec(desc="build feature"),
        workflow_route=WorkflowRoute(join="tdd", leave="verify"),
        workflow_runs={
            "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.SATISFIED),
        },
    )

    state.update_after_turn(_resolution(intent=TaskIntent.GENERAL), "thanks")

    assert state.current_intent == TaskIntent.GENERAL
    assert state.current_goal is not None
    assert state.workflow_route is None
    assert state.workflow_runs == {}


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
        "review this",
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
        "continue",
    )

    assert state.current_goal == goal
    assert state.workflow_route == WorkflowRoute(join="tdd", leave="verify")
    assert state.workflow_runs == {"tdd": active}


def test_coding_turn_without_goal_keeps_existing_goal_but_clears_route():
    goal = GoalSpec(desc="build feature")
    state = TaskState(
        current_goal=goal,
        workflow_route=WorkflowRoute(join="brainstorm", leave="verify"),
    )

    state.update_after_turn(_resolution(intent=TaskIntent.CODING), "what next?")

    assert state.current_goal == goal
    assert state.workflow_route is None


def test_intent_window_keeps_recent_user_inputs():
    state = TaskState(
        recent_exchanges=[
            TurnExchange(user_text="first", assistant_text=""),
            TurnExchange(user_text="second", assistant_text="reply"),
            TurnExchange(user_text="third", assistant_text="reply"),
        ]
    )

    assert state.intent_window_text("fourth") == "first [SEP] second [SEP] third [SEP] fourth"

    state2 = TaskState(
        recent_exchanges=[
            TurnExchange(user_text="first", assistant_text=""),
            TurnExchange(user_text="second", assistant_text="reply"),
        ]
    )

    assert state2.intent_window_text("third") == "first [SEP] second [SEP] third"

    # window size 4: 5 exchanges should truncate to last 4
    state3 = TaskState(
        recent_exchanges=[
            TurnExchange(user_text="a", assistant_text=""),
            TurnExchange(user_text="b", assistant_text=""),
            TurnExchange(user_text="c", assistant_text=""),
            TurnExchange(user_text="d", assistant_text=""),
        ]
    )

    assert state3.intent_window_text("e") == "b [SEP] c [SEP] d [SEP] e"


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
    assert state.current_intent == TaskIntent.CODING
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
