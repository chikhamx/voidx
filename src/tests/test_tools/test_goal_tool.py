from types import SimpleNamespace

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.goal import GoalTool


class Controller:
    def __init__(self):
        self.calls = []

    async def submit_decision(self, decision):
        self.calls.append(decision)
        return SimpleNamespace(outcome="completed", summary=decision["summary"])


@pytest.mark.asyncio
async def test_goal_tool_schema_exposes_lifecycle_statuses() -> None:
    schema = GoalTool().parameters_schema()
    assert schema["properties"]["status"]["enum"] == ["finished", "continue", "blocked"]


@pytest.mark.asyncio
async def test_goal_tool_is_guidance_only_outside_evaluator() -> None:
    controller = Controller()
    ctx = ToolContext(workspace="/tmp", goal_controller=controller, goal_phase="work")

    result = await GoalTool().execute(
        {"status": "finished", "summary": "done", "evidence": "verified"}, ctx
    )

    assert result.metadata["goal_decision_submitted"] is False
    assert controller.calls == []


@pytest.mark.asyncio
async def test_goal_tool_submits_only_in_evaluator_phase() -> None:
    controller = Controller()
    ctx = ToolContext(workspace="/tmp", goal_controller=controller, goal_phase="evaluator")

    result = await GoalTool().execute(
        {"status": "finished", "summary": "done", "evidence": "verified"}, ctx
    )

    assert result.metadata["goal_decision_submitted"] is True
    assert controller.calls[0]["outcome"] == "completed"
