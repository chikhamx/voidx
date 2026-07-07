"""Tests for goal_resolver retry behavior."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from voidx.agent.goal_resolver import resolve_goal_for_turn, ResolverGoal
from voidx.config import RetryConfig
from voidx.runtime.intent import TaskIntent
from voidx.runtime.task_state import TaskState


def _make_task_state() -> TaskState:
    return TaskState()


def _make_model(ainvoke_side_effects):
    """Create a mock model whose ainvoke succeeds/fails per side_effects list."""
    runnable = MagicMock()
    runnable.ainvoke = AsyncMock(side_effect=ainvoke_side_effects)

    model = MagicMock()
    model.with_structured_output = MagicMock(return_value=runnable)
    return model


class TestGoalResolverRetry:
    @pytest.mark.asyncio
    async def test_retries_on_timeout_then_succeeds(self):
        task_state = _make_task_state()
        good_result = ResolverGoal(intent="coding", goal=None)
        model = _make_model([
            asyncio.TimeoutError(),
            good_result,
        ])

        rc = RetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=False)
        result = await resolve_goal_for_turn(
            model=model,
            user_text="do something",
            interaction_mode=None,
            task_state=task_state,
            retry_config=rc,
        )
        assert result.intent.type == TaskIntent.CODING

    @pytest.mark.asyncio
    async def test_falls_back_after_exhausting_retries(self):
        task_state = _make_task_state()
        model = _make_model([
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        ])

        rc = RetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=False)
        result = await resolve_goal_for_turn(
            model=model,
            user_text="do something",
            interaction_mode=None,
            task_state=task_state,
            retry_config=rc,
        )
        assert result.intent.type == TaskIntent.GENERAL

    @pytest.mark.asyncio
    async def test_no_retry_config_uses_default(self):
        task_state = _make_task_state()
        good_result = ResolverGoal(intent="coding", goal=None)
        model = _make_model([
            asyncio.TimeoutError(),
            good_result,
        ])

        result = await resolve_goal_for_turn(
            model=model,
            user_text="do something",
            interaction_mode=None,
            task_state=task_state,
        )
        assert result.intent.type == TaskIntent.CODING

    @pytest.mark.asyncio
    async def test_non_retryable_exception_no_retry(self):
        task_state = _make_task_state()
        model = _make_model([
            ValueError("not a transient error"),
        ])

        rc = RetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=False)
        result = await resolve_goal_for_turn(
            model=model,
            user_text="do something",
            interaction_mode=None,
            task_state=task_state,
            retry_config=rc,
        )
        assert result.intent.type == TaskIntent.GENERAL
