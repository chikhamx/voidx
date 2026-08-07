from __future__ import annotations

import pytest

from voidx.agent.domain.automation.goal import GOAL_PROFILE, GoalSpec, GoalState, GoalToolView


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
    assert {"workflow", "todo"}.issubset(workflow.bound_tool_ids)
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


def test_goal_tool_view_evaluator_phase_excludes_execution_and_web_tools() -> None:
    """Evaluator judges from evidence; it gets read-only tools plus goal, no mcp/web."""
    available = {
        "read", "find", "search", "lsp", "document",
        "bash", "write", "replace", "manage",
        "websearch", "webfetch", "mcp", "skill",
        "goal", "clarify",
    }

    view = GoalToolView.default(phase="evaluator").bind(available)

    assert {"read", "find", "search", "lsp", "document", "goal"}.issubset(view.bound_tool_ids)
    assert {"bash", "write", "replace", "manage", "websearch", "webfetch", "mcp", "skill", "clarify"}.isdisjoint(
        view.bound_tool_ids
    )


def test_goal_evaluator_directive_exists_for_evaluator_phase() -> None:
    from voidx.agent.domain.automation.goal import GOAL_EVALUATOR_DIRECTIVE

    assert "decision" in GOAL_EVALUATOR_DIRECTIVE
    assert "evaluator" in GOAL_EVALUATOR_DIRECTIVE.lower()


def test_goal_phase_directive_covers_intake_and_evaluator() -> None:
    from types import SimpleNamespace

    from voidx.agent.domain.automation.goal import GOAL_EVALUATOR_DIRECTIVE, GOAL_INTAKE_DIRECTIVE
    from voidx.agent.domain.automation.goal import GoalPromptPolicy

    policy = GoalPromptPolicy()

    intake_ctx = SimpleNamespace(goal_phase="intake")
    evaluator_ctx = SimpleNamespace(goal_phase="evaluator")
    work_ctx = SimpleNamespace(goal_phase="work")

    assert policy.profile_sections(intake_ctx)[0].content == GOAL_INTAKE_DIRECTIVE
    assert policy.profile_sections(evaluator_ctx)[0].content == GOAL_EVALUATOR_DIRECTIVE
    assert policy.profile_sections(work_ctx) == []
    assert policy.profile_sections(None) == []


def test_goal_tool_view_idle_phase_binds_readonly_clarify_and_goal() -> None:
    available = {
        "read", "find", "search", "lsp", "document",
        "bash", "write", "replace", "manage", "lsp_format",
        "websearch", "webfetch", "mcp", "skill",
        "clarify", "goal", "checkpoint", "turn", "loop", "workflow", "todo",
    }

    view = GoalToolView.default(phase="idle").bind(available)

    assert {"read", "find", "search", "lsp", "document", "clarify", "goal"}.issubset(
        view.bound_tool_ids
    )
    # no execution or write tools in idle
    assert {"bash", "write", "replace", "manage", "lsp_format"}.isdisjoint(view.bound_tool_ids)
    # no web/mcp/skill in idle
    assert {"websearch", "webfetch", "mcp", "skill"}.isdisjoint(view.bound_tool_ids)
    # no interactive protocol tools besides clarify/goal
    assert {"checkpoint", "turn", "loop", "workflow", "todo"}.isdisjoint(view.bound_tool_ids)


def test_goal_tool_view_idle_phase_denies_bash() -> None:
    view = GoalToolView.default(phase="idle").bind({"bash", "read", "goal"})

    assert view.check_tool_call("bash", {"command": "pytest -q"}).allowed is False
    assert view.check_tool_call("read", {"file_path": "/tmp/x"}).allowed is True


def test_goal_idle_directive_exists_for_idle_phase() -> None:
    from voidx.agent.domain.automation.goal import GOAL_IDLE_DIRECTIVE

    assert "goal" in GOAL_IDLE_DIRECTIVE.lower()
    assert "init" in GOAL_IDLE_DIRECTIVE.lower()


def test_goal_phase_directive_covers_idle() -> None:
    from types import SimpleNamespace

    from voidx.agent.domain.automation.goal import GOAL_IDLE_DIRECTIVE
    from voidx.agent.domain.automation.goal import GoalPromptPolicy

    idle_ctx = SimpleNamespace(goal_phase="idle")

    assert GoalPromptPolicy().profile_sections(idle_ctx)[0].content == GOAL_IDLE_DIRECTIVE
