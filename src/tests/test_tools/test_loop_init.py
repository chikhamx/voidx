"""Tests for loop tool op=init — structured LoopSpec submission with user approval."""

from __future__ import annotations

import pytest

from voidx.agent.domain.loop import LoopSpec
from voidx.agent.loop.intake_controller import LoopIntakeController
from voidx.tools.base import ToolContext, UserInteraction
from voidx.tools.loop import LoopTool


class FakeInteraction:
    def __init__(self, *, value: str = "approved", free_text: bool = False, cancelled: bool = False):
        self._value = value
        self._free_text = free_text
        self._cancelled = cancelled

    async def __call__(self, interaction: UserInteraction):
        from voidx.tools.base import UserResponse
        return UserResponse(
            value=self._value,
            free_text=self._free_text,
            cancelled=self._cancelled,
        )


@pytest.mark.asyncio
async def test_init_submits_spec_to_intake_controller():
    controller = LoopIntakeController()
    tool = LoopTool()
    ctx = ToolContext(
        workspace="/tmp/workspace",
        loop_intake_controller=controller,
        loop_phase="idle",
        interact=FakeInteraction(value="approved"),
    )

    result = await tool.execute(
        {"operation": "init", "prompt": "Monitor build status", "interval_seconds": 60},
        ctx,
    )

    assert result.metadata["loop_init_submitted"] is True
    assert result.metadata["loop_init_decision"] == "approved"
    spec = controller.final_spec()
    assert spec is not None
    assert spec.prompt == "Monitor build status"
    assert spec.interval_seconds == 60


@pytest.mark.asyncio
async def test_init_rejected_when_not_idle_phase():
    controller = LoopIntakeController()
    tool = LoopTool()
    ctx = ToolContext(
        workspace="/tmp/workspace",
        loop_intake_controller=controller,
        loop_phase="work",
        interact=FakeInteraction(value="approved"),
    )

    result = await tool.execute(
        {"operation": "init", "prompt": "Monitor build status"},
        ctx,
    )

    assert result.metadata["loop_init_submitted"] is False
    assert result.metadata.get("guidance_only") is True
    assert controller.final_spec() is None


@pytest.mark.asyncio
async def test_init_rejected_when_no_intake_controller():
    tool = LoopTool()
    ctx = ToolContext(
        workspace="/tmp/workspace",
        loop_phase="idle",
        interact=FakeInteraction(value="approved"),
    )

    result = await tool.execute(
        {"operation": "init", "prompt": "Monitor build status"},
        ctx,
    )

    assert result.metadata["loop_init_submitted"] is False
    assert result.metadata.get("guidance_only") is True


@pytest.mark.asyncio
async def test_init_cancelled_by_user():
    controller = LoopIntakeController()
    tool = LoopTool()
    ctx = ToolContext(
        workspace="/tmp/workspace",
        loop_intake_controller=controller,
        loop_phase="idle",
        interact=FakeInteraction(value="cancelled"),
    )

    result = await tool.execute(
        {"operation": "init", "prompt": "Monitor build status"},
        ctx,
    )

    assert result.metadata["loop_init_submitted"] is False
    assert result.metadata["loop_init_decision"] == "cancelled"
    assert controller.cancelled is True
    assert controller.final_spec() is None


@pytest.mark.asyncio
async def test_init_revise_feedback_not_submitted():
    controller = LoopIntakeController()
    tool = LoopTool()
    ctx = ToolContext(
        workspace="/tmp/workspace",
        loop_intake_controller=controller,
        loop_phase="idle",
        interact=FakeInteraction(value="make it faster", free_text=True),
    )

    result = await tool.execute(
        {"operation": "init", "prompt": "Monitor build status"},
        ctx,
    )

    assert result.metadata["loop_init_submitted"] is False
    assert result.metadata["loop_init_decision"] == "revised"
    assert controller.final_spec() is None


@pytest.mark.asyncio
async def test_init_auto_approved_when_no_interaction():
    controller = LoopIntakeController()
    tool = LoopTool()
    ctx = ToolContext(
        workspace="/tmp/workspace",
        loop_intake_controller=controller,
        loop_phase="idle",
    )

    result = await tool.execute(
        {"operation": "init", "prompt": "Monitor build status"},
        ctx,
    )

    assert result.metadata["loop_init_submitted"] is True
    assert result.metadata["loop_init_decision"] == "auto_approved"
    assert controller.final_spec() is not None


@pytest.mark.asyncio
async def test_init_rejects_empty_prompt():
    controller = LoopIntakeController()
    tool = LoopTool()
    ctx = ToolContext(
        workspace="/tmp/workspace",
        loop_intake_controller=controller,
        loop_phase="idle",
        interact=FakeInteraction(value="approved"),
    )

    result = await tool.execute(
        {"operation": "init", "prompt": "   "},
        ctx,
    )

    assert result.metadata.get("error") is True
    assert controller.final_spec() is None
