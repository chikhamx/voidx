"""Tests for ToolView requests_approval bypassing the short-circuit."""

from __future__ import annotations

from tests.langgraph_execution import make_langgraph_execution
import pytest

from voidx.agent.domain.automation.goal import GoalToolView
from voidx.agent.adapters.langgraph.runtime.thread_context import (
    ThreadExecutionState,
    _CURRENT_THREAD_EXECUTION_STATE,
)


def _graph(workspace):
    from voidx.config import Config
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution

    cfg = Config(workspace=str(workspace))
    return make_langgraph_execution(cfg, api_key="test")


@pytest.mark.asyncio
async def test_goal_tool_view_bash_not_short_circuited(tmp_path):
    """When GoalToolView marks bash requests_approval, _authorize_tool_calls
    must not short-circuit bash; it must defer to the global permission engine."""
    graph = _graph(tmp_path)
    graph._permission.permission_mode = "safe"

    view = GoalToolView.default(phase="work").bind({"bash", "read"})
    state = ThreadExecutionState(tool_policy=view)
    token = _CURRENT_THREAD_EXECUTION_STATE.set(state)
    try:
        approved, denied = await graph._authorize_tool_calls(
            [{"name": "bash", "args": {"command": "pytest -q"}, "id": "call_1"}],
            plan_mode=False,
            session_id="test",
        )
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    assert approved == []
    assert len(denied) == 1
    assert denied[0][0]["name"] == "bash"


@pytest.mark.asyncio
async def test_goal_tool_view_read_still_short_circuited(tmp_path):
    """Non-bash tools bound by GoalToolView should still be short-circuited."""
    graph = _graph(tmp_path)
    graph._permission.permission_mode = "safe"

    view = GoalToolView.default(phase="work").bind({"bash", "read"})
    state = ThreadExecutionState(tool_policy=view)
    token = _CURRENT_THREAD_EXECUTION_STATE.set(state)
    try:
        approved, denied = await graph._authorize_tool_calls(
            [{"name": "read", "args": {"file_path": "/tmp/x"}, "id": "call_1"}],
            plan_mode=False,
            session_id="test",
        )
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    assert len(approved) == 1
    assert approved[0]["name"] == "read"
    assert denied == []


@pytest.mark.asyncio
async def test_goal_tool_view_bash_approved_when_user_says_yes(tmp_path):
    """When the user approves bash via the frontend, it should be allowed."""
    graph = _graph(tmp_path)
    graph._permission.permission_mode = "safe"

    async def approve(_tool_calls):
        return "y"

    graph._ask_tool_permission = approve

    view = GoalToolView.default(phase="work").bind({"bash", "read"})
    state = ThreadExecutionState(tool_policy=view)
    token = _CURRENT_THREAD_EXECUTION_STATE.set(state)
    try:
        approved, denied = await graph._authorize_tool_calls(
            [{"name": "bash", "args": {"command": "pytest -q"}, "id": "call_1"}],
            plan_mode=False,
            session_id="test",
        )
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    assert len(approved) == 1
    assert approved[0]["name"] == "bash"
    assert denied == []


@pytest.mark.asyncio
async def test_mixed_read_and_bash_preserves_read_approval(tmp_path):
    """When read (short-circuited) and bash (deferred to engine) are in the
    same batch, read must remain approved after bash goes through the engine."""
    graph = _graph(tmp_path)
    graph._permission.permission_mode = "safe"

    async def approve(_tool_calls):
        return "y"

    graph._ask_tool_permission = approve

    view = GoalToolView.default(phase="work").bind({"bash", "read"})
    state = ThreadExecutionState(tool_policy=view)
    token = _CURRENT_THREAD_EXECUTION_STATE.set(state)
    try:
        approved, denied = await graph._authorize_tool_calls(
            [
                {"name": "read", "args": {"file_path": "/tmp/x"}, "id": "call_read"},
                {"name": "bash", "args": {"command": "pytest -q"}, "id": "call_bash"},
            ],
            plan_mode=False,
            session_id="test",
        )
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    approved_names = sorted(tc["name"] for tc in approved)
    assert approved_names == ["bash", "read"]
    assert denied == []
