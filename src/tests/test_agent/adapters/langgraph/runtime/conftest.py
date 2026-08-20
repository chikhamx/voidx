"""Shared fixtures for direct graph-node tests."""

import pytest

from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.domain.agent_profile import WorkflowRuntimeContext
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
from voidx.agent.adapters.langgraph.runtime.thread_context import (
    ThreadExecutionState,
    _CURRENT_THREAD_EXECUTION_STATE,
)

import pytest_asyncio

from voidx.presentation.output.dock import set_dock
from voidx.presentation.output.events import ui_events




@pytest.fixture(autouse=True)
def bind_subagent_scoped_tools(monkeypatch):
    from voidx.agent.adapters.langgraph.runtime import subagent
    from voidx.bootstrap.tooling import bind_scoped_tools

    monkeypatch.setattr(subagent, "bind_scoped_tools", bind_scoped_tools)

@pytest_asyncio.fixture(autouse=True)
async def reset_presentation_event_owner():
    yield
    if ui_events.is_running:
        await ui_events.stop()
    set_dock(None)






@pytest.fixture(autouse=True)
def bound_turn_execution_context(request, tmp_path):
    source = request.path.read_text(encoding="utf-8")
    if "_execute_tools(" not in source:
        yield
        return
    state = ThreadExecutionState(
        thread_id="test-thread",
        turn_context=TurnExecutionContext(
            thread_id="test-thread",
            session_id="test-session",
            workspace=str(tmp_path),
            workflow_context=WorkflowRuntimeContext(
                dag=DEFAULT_WORKFLOW_DAG,
                dag_revision=1,
                dag_hash="test-default-workflow",
                source="bundled",
            ),
        ),
        workspace=str(tmp_path),
    )
    token = _CURRENT_THREAD_EXECUTION_STATE.set(state)
    try:
        yield
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)
