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
async def test_profile_policy_rechecks_registry_capability_before_authorization(tmp_path):
    from voidx.agent.domain.agent_profile import ResourcePolicy
    from voidx.agent.domain.run_config import resolve_run_config
    from voidx.agent.domain.tool_policy import CodingToolPolicy, ProfileToolPolicy

    graph = _graph(tmp_path)
    policy = ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(hitl_mode="autonomous"),
        run_config=resolve_run_config("single"),
        snapshot_hash="snapshot-1",
        phase="turn",
    )
    state = ThreadExecutionState(tool_policy=policy)
    token = _CURRENT_THREAD_EXECUTION_STATE.set(state)
    try:
        approved, denied = await graph._authorize_tool_calls(
            [{"name": "clarify", "args": {"question": "continue?"}, "id": "call_1"}],
            plan_mode=False,
            session_id="test",
        )
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    assert approved == []
    assert len(denied) == 1
    assert denied[0][1] == "Tool denied: hitl_interaction_unavailable"


@pytest.mark.asyncio
async def test_default_profile_policy_does_not_short_circuit_authorization(tmp_path):
    from voidx.agent.application.agent_registry import AgentRegistry
    from voidx.agent.application.profile_tool_policy import (
        default_profile_tool_policy_for,
    )

    graph = _graph(tmp_path)
    graph._permission.permission_mode = "safe"
    resolved = AgentRegistry(str(tmp_path)).resolve("coding")
    state = ThreadExecutionState(
        tool_policy=default_profile_tool_policy_for(resolved)
    )
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


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["ask", "blocked_ack"])
async def test_autonomous_profile_rejects_interactive_authorization_without_prompt(
    tmp_path, monkeypatch, action
):
    from voidx.agent.adapters.langgraph.runtime import permission_flow
    from voidx.agent.domain.agent_profile import ResourcePolicy
    from voidx.agent.domain.run_config import resolve_run_config
    from voidx.agent.domain.tool_policy import CodingToolPolicy, ProfileToolPolicy
    from voidx.tooling.application.permission_service import classify_tool_call
    from voidx.tooling.domain.authorization import PermissionDecision
    from voidx.tooling.domain.permission import Action

    graph = _graph(tmp_path)
    tool_call = {
        "name": "bash",
        "args": {"command": "pytest -q"},
        "id": "call_1",
    }
    classified = classify_tool_call(tool_call)
    decision = PermissionDecision(
        action=Action(action),
        tool_call=classified.tool_call,
        name=classified.name,
        args=classified.args,
        pattern=classified.pattern,
        capability=classified.capability,
        reason=f"authorization_{action}",
    )
    monkeypatch.setattr(permission_flow, "authorize_tool_call", lambda *_: decision)
    prompt_calls = 0

    async def fail_if_prompted(_tool_calls):
        nonlocal prompt_calls
        prompt_calls += 1
        raise AssertionError("autonomous authorization must not prompt")

    graph._ask_tool_permission = fail_if_prompted
    policy = ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(hitl_mode="autonomous"),
        run_config=resolve_run_config("single"),
        snapshot_hash="snapshot-1",
        phase="turn",
    )
    state = ThreadExecutionState(tool_policy=policy)
    token = _CURRENT_THREAD_EXECUTION_STATE.set(state)
    try:
        approved, denied = await graph._authorize_tool_calls(
            [tool_call],
            plan_mode=False,
            session_id="test",
        )
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    assert approved == []
    assert denied == [
        (decision.tool_call, f"Autonomous authorization denied: {action}")
    ]
    assert prompt_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["call", "CALL", " Call "])
async def test_autonomous_mcp_call_normalizes_operation_before_execution_gate(
    tmp_path, monkeypatch, op
):
    from voidx.agent.adapters.langgraph.runtime import permission_flow
    from voidx.agent.domain.agent_profile import ResourcePolicy
    from voidx.agent.domain.run_config import resolve_run_config
    from voidx.agent.domain.tool_policy import CodingToolPolicy, ProfileToolPolicy
    from voidx.tooling.application.authorization import authorize_tool_call as real_authorize

    graph = _graph(tmp_path)
    contexts = []

    def capture_authorization(tool_call, context):
        contexts.append(context)
        return real_authorize(tool_call, context)

    monkeypatch.setattr(permission_flow, "authorize_tool_call", capture_authorization)

    async def fail_if_prompted(_tool_calls):
        raise AssertionError("autonomous MCP authorization must not prompt")

    graph._ask_tool_permission = fail_if_prompted
    policy = ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(
            hitl_mode="autonomous",
            mcp_servers=("github",),
        ),
        run_config=resolve_run_config("single"),
        snapshot_hash="snapshot-mcp",
        phase="turn",
    )
    state = ThreadExecutionState(tool_policy=policy)
    token = _CURRENT_THREAD_EXECUTION_STATE.set(state)
    try:
        approved, denied = await graph._authorize_tool_calls(
            [{
                "name": "mcp",
                "args": {
                    "op": op,
                    "server": "github",
                    "tool": "create_issue",
                    "arguments": {"title": "Bug"},
                },
                "id": "call_mcp",
            }],
            plan_mode=False,
            session_id="test",
        )
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    assert approved == []
    assert len(denied) == 1
    assert denied[0][1] == "Autonomous authorization denied: ask"
    assert len(contexts) == 1
    assert contexts[0].execution_gated is True
