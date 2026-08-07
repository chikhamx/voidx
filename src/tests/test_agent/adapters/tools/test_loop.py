from __future__ import annotations

import pytest

from voidx.agent.domain.automation.loop import LoopDecision, LoopMode, LoopSpec
from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime
from voidx.agent.adapters.tools.automation.loop import LoopTool


class FakeLoopController:
    def __init__(self, spec: LoopSpec | None = None) -> None:
        self.spec = spec or LoopSpec(prompt="check deploy")
        self.decisions: list[LoopDecision] = []

    async def submit_decision(self, decision) -> LoopDecision:
        loop_decision = LoopDecision.model_validate(decision)
        self.decisions.append(loop_decision)
        if self.spec.mode is LoopMode.FIXED:
            return loop_decision.model_copy(update={"next_delay_seconds": self.spec.interval_seconds})
        if loop_decision.outcome == "continue" and loop_decision.next_delay_seconds is None:
            return loop_decision.model_copy(update={"next_delay_seconds": 600.0})
        return loop_decision

    def final_decision(self) -> LoopDecision | None:
        return self.decisions[-1] if self.decisions else None


@pytest.mark.asyncio
async def test_loop_requires_loop_controller() -> None:
    tool = LoopTool()
    ctx = ToolContext(workspace="/tmp/workspace")

    result = await tool.execute({"outcome": "continue", "summary": "done"}, ctx)

    assert result.metadata["error"] is True
    assert "No active runtime-backed /loop" in result.output



@pytest.mark.asyncio
async def test_loop_start_ignores_non_required_noise_fields() -> None:
    controller = FakeLoopController()
    tool = LoopTool()
    ctx = ToolContext(workspace="/tmp/workspace", runtime=AgentToolRuntime(loop_control=controller))

    result = await tool.execute(
        {
            "operation": "start",
            "goal": "use typex mcp",
            "outcome": "null",
            "summary": None,
            "progress": "definitely-not-a-progress-value",
            "next_delay_seconds": "null",
        },
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert result.metadata["operation"] == "start"
    assert result.metadata["goal"] == "use typex mcp"
    assert controller.decisions == []
@pytest.mark.asyncio
async def test_loop_submits_continue_decision() -> None:
    controller = FakeLoopController()
    tool = LoopTool()
    ctx = ToolContext(workspace="/tmp/workspace", runtime=AgentToolRuntime(loop_control=controller))

    result = await tool.execute(
        {"outcome": "continue", "summary": "checked", "next_delay_seconds": 120}, ctx
    )

    assert controller.decisions == [
        LoopDecision(outcome="continue", summary="checked", next_delay_seconds=120)
    ]
    assert result.metadata["outcome"] == "continue"
    assert result.metadata["next_delay_seconds"] == 120


@pytest.mark.asyncio
async def test_loop_ignores_dynamic_delay_for_fixed_loop() -> None:
    controller = FakeLoopController(LoopSpec(prompt="check deploy", interval_seconds=300))
    tool = LoopTool()
    ctx = ToolContext(workspace="/tmp/workspace", runtime=AgentToolRuntime(loop_control=controller))

    result = await tool.execute(
        {"outcome": "continue", "summary": "checked", "next_delay_seconds": 30}, ctx
    )

    assert result.metadata["mode"] == "fixed"
    assert result.metadata["next_delay_seconds"] == 300


# ── Model cannot end the loop ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "stop", "failed", "blocked", "needs_user"])
async def test_loop_rejects_terminal_outcomes_from_model(outcome: str) -> None:
    controller = FakeLoopController()
    tool = LoopTool()
    ctx = ToolContext(workspace="/tmp/workspace", runtime=AgentToolRuntime(loop_control=controller))

    result = await tool.execute({"outcome": outcome, "summary": "done"}, ctx)

    assert result.metadata.get("error") is True
    assert controller.decisions == []


@pytest.mark.asyncio
async def test_loop_rejects_stop_operation_from_model() -> None:
    controller = FakeLoopController()
    tool = LoopTool()
    ctx = ToolContext(workspace="/tmp/workspace", runtime=AgentToolRuntime(loop_control=controller))

    result = await tool.execute({"operation": "stop", "summary": "done"}, ctx)

    assert result.metadata.get("error") is True
    assert controller.decisions == []


def test_loop_schema_exposes_no_terminal_outcomes() -> None:
    import json

    schema = json.dumps(LoopTool().parameters_schema())

    for forbidden in ("completed", "stop", "failed", "blocked", "needs_user"):
        assert f'"{forbidden}"' not in schema


@pytest.mark.asyncio
async def test_loop_start_state_patch_goal_syncs_task_state() -> None:
    controller = FakeLoopController()
    tool = LoopTool()
    ctx = ToolContext(workspace="/tmp/workspace", runtime=AgentToolRuntime(loop_control=controller))

    result = await tool.execute({"operation": "start", "goal": "ship retry"}, ctx)

    assert result.metadata["operation"] == "start"
    patch = result.metadata.get("state_patch")
    assert patch is not None
    assert patch["goal"]["desc"] == "ship retry"
