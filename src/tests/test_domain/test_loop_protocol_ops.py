"""Loop tool lifecycle operations: start declares intent, commit submits the iteration decision, stop ends the loop."""

from __future__ import annotations

import pytest

from voidx.agent.domain.automation.loop import LoopDecision, LoopSpec
from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime
from voidx.agent.adapters.tools.automation.loop import LoopTool

from tests.test_agent.adapters.tools.test_loop import FakeLoopController


def _ctx(controller) -> ToolContext:
    return ToolContext(workspace="/tmp/workspace", runtime=AgentToolRuntime(loop_control=controller))


@pytest.mark.asyncio
async def test_loop_start_declares_intent_without_decision() -> None:
    controller = FakeLoopController()
    tool = LoopTool()

    result = await tool.execute(
        {"operation": "start", "goal": "检查 TypeX 提及"}, _ctx(controller)
    )

    assert result.metadata.get("error") is not True
    assert result.metadata["operation"] == "start"
    assert result.metadata["goal"] == "检查 TypeX 提及"
    assert controller.decisions == []


@pytest.mark.asyncio
async def test_loop_start_requires_goal() -> None:
    controller = FakeLoopController()
    tool = LoopTool()

    result = await tool.execute({"operation": "start"}, _ctx(controller))

    assert result.metadata["error"] is True
    assert "goal" in result.output


@pytest.mark.asyncio
async def test_loop_commit_submits_iteration_decision() -> None:
    controller = FakeLoopController()
    tool = LoopTool()

    result = await tool.execute(
        {"operation": "commit", "outcome": "continue", "summary": "checked", "next_delay_seconds": 120},
        _ctx(controller),
    )

    assert controller.decisions == [
        LoopDecision(outcome="continue", summary="checked", next_delay_seconds=120)
    ]
    assert result.metadata["outcome"] == "continue"


@pytest.mark.asyncio
async def test_loop_commit_requires_decision_fields() -> None:
    controller = FakeLoopController()
    tool = LoopTool()

    result = await tool.execute({"operation": "commit"}, _ctx(controller))

    assert result.metadata["error"] is True
    assert controller.decisions == []


@pytest.mark.asyncio
async def test_loop_bare_decision_still_supported_for_compat() -> None:
    controller = FakeLoopController()
    tool = LoopTool()

    result = await tool.execute({"outcome": "continue", "summary": "done"}, _ctx(controller))

    assert result.metadata.get("error") is not True
    assert result.metadata["outcome"] == "continue"
