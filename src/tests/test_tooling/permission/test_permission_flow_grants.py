"""Tests for permission_flow grant application based on user choice."""

from __future__ import annotations

from tests.langgraph_execution import make_langgraph_execution
import pytest


def _graph(workspace):
    from voidx.config import Config
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution
    cfg = Config(workspace=str(workspace))
    return make_langgraph_execution(cfg, api_key="test")


def _external_write_decision(graph, workspace, target):
    from voidx.tooling.domain.authorization import PermissionContext
    from voidx.tooling.application.authorization import authorize_tool_call
    ctx = graph._permission.context_for(
        workspace=str(workspace),
        interaction_mode="auto",
        plan_mode=False,
    )
    return authorize_tool_call(
        {"name": "write", "args": {"file_path": str(target), "op": "write", "new_string": "x"}, "id": "call_1"},
        ctx,
    )


@pytest.mark.asyncio
async def test_session_file_choice_writes_session_grant(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "out.txt"

    graph = _graph(workspace)
    graph._permission.permission_mode = "safe"
    decision = _external_write_decision(graph, workspace, target)
    assert decision.access_intents

    async def approve(_tool_calls):
        return "session_file"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": str(target), "op": "write", "new_string": "x"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
    )

    assert len(approved) == 1
    assert denied == []
    grants = tuple(g for g in graph._permission.grant_snapshot() if g.persistence == "session")
    assert any(g.object_type == "file" and str(target) in g.path for g in grants)


@pytest.mark.asyncio
async def test_persistent_file_choice_writes_persistent_grant(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "out.txt"

    graph = _graph(workspace)
    graph._permission.permission_mode = "safe"
    decision = _external_write_decision(graph, workspace, target)
    assert decision.access_intents

    async def approve(_tool_calls):
        return "persistent_file"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": str(target), "op": "write", "new_string": "x"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
    )

    assert len(approved) == 1
    grants = tuple(g for g in graph._permission.grant_snapshot() if g.persistence == "persistent")
    assert any(g.object_type == "file" and str(target) in g.path for g in grants)


@pytest.mark.asyncio
async def test_allow_once_choice_writes_runtime_grant(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "out.txt"

    graph = _graph(workspace)
    graph._permission.permission_mode = "safe"
    decision = _external_write_decision(graph, workspace, target)
    assert decision.access_intents

    async def approve(_tool_calls):
        return "allow"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": str(target), "op": "write", "new_string": "x"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
    )

    assert len(approved) == 1
    grants = tuple(g for g in graph._permission.grant_snapshot() if g.persistence == "runtime")
    assert any(g.object_type == "file" and str(target) in g.path for g in grants)


@pytest.mark.asyncio
async def test_deny_choice_denies_tool(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "out.txt"

    graph = _graph(workspace)
    graph._permission.permission_mode = "safe"

    async def approve(_tool_calls):
        return "deny"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": str(target), "op": "write", "new_string": "x"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
    )

    assert approved == []
    assert len(denied) == 1
