import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.task_state import (
    GoalResolution,
    GoalSpec,
    GoalType,
    IntentResolution,
    PlanResolution,
    TaskState,
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
        intent=IntentResolution(type=intent, desc="test resolution"),
        goal=goal,
        plan=plan,
    )


def test_update_after_turn_records_intent_goal_and_route():
    state = TaskState()
    goal = GoalSpec(type=GoalType.REVIEW, desc="review diff")
    resolution = _resolution(
        goal=goal,
        plan=PlanResolution(join="review", leave="review"),
    )

    state.update_after_turn(resolution, "review this")

    assert state.previous_intent == TaskIntent.CODING
    assert state.current_intent == TaskIntent.CODING
    assert state.current_goal == goal
    assert state.workflow_route == WorkflowRoute(join="review", leave="review")
    assert state.recent_user_texts == ["review this"]


def test_general_turn_clears_current_goal_and_route():
    state = TaskState(
        current_goal=GoalSpec(type=GoalType.FEATURE, desc="build feature"),
        workflow_route=WorkflowRoute(join="tdd", leave="verify"),
    )

    state.update_after_turn(_resolution(intent=TaskIntent.GENERAL), "thanks")

    assert state.current_intent == TaskIntent.GENERAL
    assert state.current_goal is None
    assert state.workflow_route is None


def test_coding_turn_without_goal_keeps_existing_goal_but_clears_route():
    goal = GoalSpec(type=GoalType.FEATURE, desc="build feature")
    state = TaskState(
        current_goal=goal,
        workflow_route=WorkflowRoute(join="brainstorm", leave="verify"),
    )

    state.update_after_turn(_resolution(intent=TaskIntent.CODING), "what next?")

    assert state.current_goal == goal
    assert state.workflow_route is None


def test_intent_window_keeps_only_two_recent_user_inputs():
    state = TaskState()

    for text in ["first", "second", "third"]:
        state.update_after_turn(_resolution(), text)

    assert state.recent_user_texts == ["second", "third"]
    assert state.intent_window_text("fourth") == "third [SEP] fourth"


def test_set_goal_from_string_infers_goal_and_resets_workflow_context():
    state = TaskState(
        current_goal=GoalSpec(type=GoalType.REVIEW, desc="review diff"),
        workflow_route=WorkflowRoute(join="review", leave="review"),
        workflow_runs={
            "review": WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE),
        },
    )

    state.set_goal("修复 bug")

    assert state.current_goal is not None
    assert state.current_goal.type == GoalType.BUGFIX
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
    goal = GoalSpec(type=GoalType.DOC, desc="write release notes")

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
