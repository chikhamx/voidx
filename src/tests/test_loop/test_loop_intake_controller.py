"""Tests for LoopIntakeController — intake-scoped controller for LoopSpec initialization."""

import pytest

from voidx.agent.domain.automation.loop import LoopSpec
from voidx.agent.application.automation.loop.intake_controller import LoopIntakeController


@pytest.mark.asyncio
async def test_submit_init_stores_first_spec():
    controller = LoopIntakeController()
    spec = LoopSpec(prompt="Monitor build status", interval_seconds=60)
    result = await controller.submit_init(spec)
    assert result is spec
    assert controller.final_spec() is spec


@pytest.mark.asyncio
async def test_submit_init_ignores_second_call():
    controller = LoopIntakeController()
    first = LoopSpec(prompt="First", interval_seconds=30)
    second = LoopSpec(prompt="Second", interval_seconds=60)
    await controller.submit_init(first)
    result = await controller.submit_init(second)
    assert result is first
    assert controller.final_spec() is first


def test_cancelled_defaults_false():
    controller = LoopIntakeController()
    assert controller.cancelled is False


def test_cancel_sets_cancelled():
    controller = LoopIntakeController()
    controller.cancel()
    assert controller.cancelled is True


def test_final_spec_none_when_not_submitted():
    controller = LoopIntakeController()
    assert controller.final_spec() is None
