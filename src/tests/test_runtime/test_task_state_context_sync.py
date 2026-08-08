"""Tests for _task_state property syncing across context boundaries.

The TUI's busy_activity_timer runs outside bind_thread_execution_context.
When it triggers a render, host._task_state must still return the current
value, not a stale _default_task_state from before the context was entered.
"""

from __future__ import annotations

import asyncio

from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.adapters.langgraph.runtime.thread_context import bind_thread_execution_context
from voidx.agent.domain.task.intent import TaskIntent
from voidx.agent.domain.task.state import GoalSpec, TaskState
from voidx.agent.domain.automation.workflow import WorkflowRunState, WorkflowRunStatus


class _FakeHost:
    """Minimal host replicating LangGraphExecution._task_state property semantics."""

    def __init__(self) -> None:
        self._workspace = ""
        self._default_task_state = TaskState()

    @property
    def _task_state(self) -> TaskState:
        from voidx.agent.adapters.langgraph.runtime.thread_context import current_thread_execution_state

        state = current_thread_execution_state()
        if state is not None:
            return state.task_state
        return self._default_task_state

    @_task_state.setter
    def _task_state(self, value: TaskState) -> None:
        from voidx.agent.adapters.langgraph.runtime.thread_context import current_thread_execution_state

        state = current_thread_execution_state()
        if state is not None:
            state.task_state = value
            self._default_task_state = value
        else:
            self._default_task_state = value


def _make_task_state() -> TaskState:
    return TaskState(
        current_intent=TaskIntent.CODING,
        current_goal=GoalSpec(desc="test goal"),
        workflow_runs={"plan": WorkflowRunState(name="plan", status=WorkflowRunStatus.ACTIVE)},
    )


async def test_set_task_state_inside_context_visible_outside() -> None:
    """Setting _task_state inside bind_thread_execution_context must also
    update _default_task_state so external readers (busy_activity_timer)
    see the latest value during graph execution.
    """
    host = _FakeHost()
    assert host._task_state.current_goal is None

    async with bind_thread_execution_context(host, session_id=""):
        ts = _make_task_state()
        host._task_state = ts

        # Inside context: reads state.task_state
        assert host._task_state.current_goal is not None
        assert host._task_state.workflow_runs

    # Outside context (simulates busy_activity_timer): reads _default_task_state
    assert host._task_state.current_goal is not None
    assert "plan" in host._task_state.workflow_runs


async def test_set_task_state_inside_context_visible_during_await() -> None:
    """During graph execution (await yields to event loop), an external
    asyncio task that reads host._task_state must see the value set
    inside the context, not the stale default.
    """
    host = _FakeHost()
    seen: list[str | None] = []

    async def external_reader() -> None:
        await asyncio.sleep(0.01)
        seen.append(host._task_state.current_goal.desc if host._task_state.current_goal else None)

    reader_task = asyncio.create_task(external_reader())

    async with bind_thread_execution_context(host, session_id=""):
        host._task_state = _make_task_state()
        await asyncio.sleep(0.02)

    await reader_task
    assert seen == ["test goal"]


async def test_empty_turn_context_workspace_falls_back_to_host_workspace(tmp_path) -> None:
    host = _FakeHost()
    host._workspace = str(tmp_path)

    async with bind_thread_execution_context(
        host,
        turn_context=TurnExecutionContext(thread_id="coding", session_id="", workspace=""),
    ) as state:
        assert state.workspace == str(tmp_path)
        assert state.turn_context is not None
        assert state.turn_context.workspace == str(tmp_path)
