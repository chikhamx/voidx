from __future__ import annotations

import pytest

from voidx.agent.domain.goal import GOAL_PROFILE, GoalSpec, GoalState, GoalToolView


def test_goal_profile_is_first_class() -> None:
    assert GOAL_PROFILE.profile_id == "goal"
    assert GOAL_PROFILE.name == "Goal"
    assert GOAL_PROFILE.protocol == "goal"


def test_goal_spec_requires_objective_and_acceptance_condition() -> None:
    with pytest.raises(ValueError):
        GoalSpec(objective=" ", acceptance_condition="tests pass")
    with pytest.raises(ValueError):
        GoalSpec(objective="ship feature", acceptance_condition=" ")


def test_goal_spec_validates_budget_and_isolates_identity() -> None:
    spec = GoalSpec(
        objective="ship feature",
        acceptance_condition="targeted tests pass",
        achievement_method="start with a failing test",
        max_attempts=12,
        generation="run-1",
    )

    assert spec.goal_thread_id("parent") == "goal:parent:run-1"
    assert spec.goal_session_id("parent") == "goal:parent:run-1"
    assert spec.max_attempts == 12
    with pytest.raises(ValueError):
        GoalSpec(objective="x", acceptance_condition="y", max_attempts=0)
    with pytest.raises(ValueError):
        GoalSpec(objective="x", acceptance_condition="y", max_attempts=201)


def test_goal_state_keeps_contract_immutable() -> None:
    state = GoalState.from_spec(
        GoalSpec(
            objective="ship feature",
            acceptance_condition="tests pass",
            achievement_method="use TDD",
            generation="run-1",
        ),
        run_id="run-id",
    )

    assert state.objective == "ship feature"
    assert state.attempt_count == 0
    with pytest.raises(Exception):
        state.objective = "different"


def test_goal_tool_view_excludes_interactive_protocol_tools() -> None:
    available = {
        "read",
        "search",
        "bash",
        "write",
        "replace",
        "manage",
        "websearch",
        "workflow",
        "todo",
        "task_status",
        "clarify",
        "checkpoint",
        "turn",
        "loop",
        "goal",
    }

    default = GoalToolView.default(workflow_enabled=False).bind(available)
    workflow = GoalToolView.default(workflow_enabled=True).bind(available)

    assert {"read", "search", "bash", "write", "replace", "manage", "websearch"}.issubset(
        default.bound_tool_ids
    )
    assert {"clarify", "checkpoint", "turn", "loop", "goal", "workflow", "todo"}.isdisjoint(
        default.bound_tool_ids
    )
    assert {"workflow", "todo", "task_status"}.issubset(workflow.bound_tool_ids)
    assert {"clarify", "checkpoint", "turn", "loop", "goal"}.isdisjoint(workflow.bound_tool_ids)


def test_goal_tool_view_bash_requests_approval() -> None:
    view = GoalToolView.default(phase="work").bind({"bash", "read", "write"})

    bash_decision = view.check_tool_call("bash", {"command": "pytest -q"})
    assert bash_decision.allowed is True
    assert bash_decision.requests_approval is True

    read_decision = view.check_tool_call("read", {"file_path": "/tmp/x"})
    assert read_decision.allowed is True
    assert read_decision.requests_approval is False

    write_decision = view.check_tool_call("write", {"file_path": "/tmp/x"})
    assert write_decision.allowed is True
    assert write_decision.requests_approval is False


def test_goal_tool_view_evaluator_phase_bash_not_bound() -> None:
    view = GoalToolView.default(phase="evaluator").bind({"bash", "read", "goal"})

    bash_decision = view.check_tool_call("bash", {"command": "pytest -q"})
    assert bash_decision.allowed is False


def test_goal_tool_view_intake_phase_binds_clarify_and_goal() -> None:
    view = GoalToolView.default(phase="intake").bind({"read", "clarify", "goal", "bash", "write"})

    assert view.allows("read") is True
    assert view.allows("clarify") is True
    assert view.allows("goal") is True
    assert view.allows("bash") is False
    assert view.allows("write") is False
