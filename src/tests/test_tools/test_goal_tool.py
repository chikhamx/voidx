from types import SimpleNamespace

import pytest

from voidx.agent.domain.goal import GoalSpec
from voidx.tools.base import ToolContext
from voidx.tools.goal import GoalTool


class DecisionController:
    def __init__(self):
        self.calls = []

    async def submit_decision(self, decision):
        self.calls.append(decision)
        return SimpleNamespace(outcome="completed", summary=decision["summary"])


class IntakeController:
    def __init__(self):
        self.spec = None

    async def submit_init(self, spec):
        self.spec = spec
        return spec

    def final_spec(self):
        return self.spec


@pytest.mark.asyncio
async def test_goal_tool_schema_requires_explicit_op() -> None:
    schema = GoalTool().parameters_schema()

    assert "op" in schema["required"]
    assert schema["properties"]["op"]["enum"] == ["init", "decision"]
    assert schema["properties"]["status"]["enum"] == ["finished", "continue", "blocked", ""]


@pytest.mark.asyncio
async def test_goal_tool_rejects_legacy_status_without_op() -> None:
    controller = DecisionController()
    ctx = ToolContext(workspace="/tmp", goal_controller=controller, goal_phase="evaluator")

    result = await GoalTool().execute(
        {"status": "finished", "summary": "done", "evidence": "verified"}, ctx
    )

    assert result.metadata["error"] is True
    assert controller.calls == []


@pytest.mark.asyncio
async def test_goal_tool_init_submits_only_in_intake_phase() -> None:
    controller = IntakeController()
    ctx = ToolContext(workspace="/tmp", goal_intake_controller=controller, goal_phase="intake")

    result = await GoalTool().execute(
        {
            "op": "init",
            "objective": "ship feature",
            "acceptance_condition": "tests pass",
            "achievement_method": "use TDD",
            "max_attempts": 12,
            "status": "",
            "summary": "",
            "evidence": "",
            "next": "",
            "reason": "",
            "progress": "none",
        },
        ctx,
    )

    assert result.metadata["goal_init_submitted"] is True
    assert isinstance(controller.final_spec(), GoalSpec)
    assert controller.final_spec().objective == "ship feature"
    assert controller.final_spec().max_attempts == 12


@pytest.mark.asyncio
async def test_goal_tool_init_rejected_outside_intake_phase() -> None:
    controller = IntakeController()
    ctx = ToolContext(workspace="/tmp", goal_intake_controller=controller, goal_phase="work")

    result = await GoalTool().execute(
        {
            "op": "init",
            "objective": "ship feature",
            "acceptance_condition": "tests pass",
            "achievement_method": "",
            "max_attempts": 20,
            "status": "",
            "summary": "",
            "evidence": "",
            "next": "",
            "reason": "",
            "progress": "none",
        },
        ctx,
    )

    assert result.metadata["goal_init_submitted"] is False
    assert controller.final_spec() is None


@pytest.mark.asyncio
async def test_goal_tool_decision_submits_only_in_evaluator_phase() -> None:
    controller = DecisionController()
    ctx = ToolContext(workspace="/tmp", goal_controller=controller, goal_phase="evaluator")

    result = await GoalTool().execute(
        {
            "op": "decision",
            "objective": "",
            "acceptance_condition": "",
            "achievement_method": "",
            "max_attempts": 20,
            "status": "finished",
            "summary": "done",
            "evidence": "verified",
            "next": "",
            "reason": "verified",
            "progress": "meaningful",
        },
        ctx,
    )

    assert result.metadata["goal_decision_submitted"] is True
    assert controller.calls[0]["outcome"] == "completed"
    assert controller.calls[0]["summary"] == "done"



@pytest.mark.asyncio
async def test_goal_tool_decision_rejects_empty_summary_without_controller_call() -> None:
    controller = DecisionController()
    ctx = ToolContext(workspace="/tmp", goal_controller=controller, goal_phase="evaluator")

    result = await GoalTool().execute(
        {
            "op": "decision",
            "objective": "",
            "acceptance_condition": "",
            "achievement_method": "",
            "max_attempts": 20,
            "status": "finished",
            "summary": "   ",
            "evidence": "verified",
            "next": "",
            "reason": "",
            "progress": "none",
        },
        ctx,
    )

    assert result.metadata["error"] is True
    assert "summary" in result.output
    assert controller.calls == []

@pytest.mark.asyncio
async def test_goal_tool_decision_rejected_outside_evaluator_phase() -> None:
    controller = DecisionController()
    ctx = ToolContext(workspace="/tmp", goal_controller=controller, goal_phase="work")

    result = await GoalTool().execute(
        {
            "op": "decision",
            "objective": "",
            "acceptance_condition": "",
            "achievement_method": "",
            "max_attempts": 20,
            "status": "finished",
            "summary": "done",
            "evidence": "verified",
            "next": "",
            "reason": "",
            "progress": "none",
        },
        ctx,
    )

    assert result.metadata["goal_decision_submitted"] is False
    assert controller.calls == []
