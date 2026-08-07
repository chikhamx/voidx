import asyncio

import pytest

from voidx.agent.gateway import AgentGateway
from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime
from voidx.agent.adapters.tools.subagent_message import MessageTool


def test_message_tool_schema_has_strict_object_properties():
    schema = MessageTool().parameters_schema()
    payload = schema["properties"]["payload"]

    assert payload["type"] == "string"
    assert "payload" in schema["required"]


@pytest.mark.asyncio
async def test_message_tool_sends_and_receives_between_child_and_parent(tmp_path):
    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-1")

    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        started.set()
        await release.wait()
        return "ok"

    spawn_task = asyncio.create_task(
        gateway.spawn(
            session_id="session-1",
            parent_run_id=root_id,
            agent_name="child",
            description="child",
            runner=runner,
        )
    )
    await started.wait()
    child = next(run for run in gateway.list_runs(session_id="session-1") if run.agent_type == "sub")

    child_ctx = ToolContext(
        workspace=str(tmp_path),
        session_id="session-1",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=child.run_id),
    )
    send_result = await MessageTool().execute(
        {
            "action": "send",
            "message_type": "question",
            "payload": {"text": "Need input"},
        },
        child_ctx,
    )

    assert send_result.metadata["message_type"] == "question"
    assert send_result.metadata["target_run_id"] == root_id

    root_messages = await gateway.receive(run_id=root_id, limit=1, timeout=0)
    assert root_messages[0].type == "question"
    assert root_messages[0].payload == {"text": "Need input"}

    root_ctx = ToolContext(
        workspace=str(tmp_path),
        session_id="session-1",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
    )
    await MessageTool().execute(
        {
            "action": "send",
            "target_run_id": child.run_id,
            "message_type": "answer",
            "payload": {"text": "Approved"},
        },
        root_ctx,
    )
    receive_result = await MessageTool().execute(
        {"action": "receive", "limit": 2, "timeout": 0},
        child_ctx,
    )

    assert receive_result.metadata["count"] == 1
    assert "Approved" in receive_result.output
    release.set()
    await spawn_task



@pytest.mark.asyncio
async def test_message_tool_accepts_json_string_payload(tmp_path):
    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-1")
    child = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="child",
        description="child",
        runner=lambda _run_id: asyncio.sleep(60),
    )
    ctx = ToolContext(
        workspace=str(tmp_path),
        session_id="session-1",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=child.run_id),
    )

    result = await MessageTool().execute(
        {
            "action": "send",
            "message_type": "progress",
            "payload": '{"step": "working"}',
        },
        ctx,
    )

    assert result.metadata["message_type"] == "progress"
    message = await gateway.receive(run_id=root_id, limit=1, timeout=0)
    assert message[0].payload == {"step": "working"}
    await gateway.cancel(requester_run_id=root_id, target_run_id=child.run_id)

@pytest.mark.asyncio
async def test_message_tool_reports_gateway_and_route_errors(tmp_path):
    result = await MessageTool().execute(
        {"action": "receive"},
        ToolContext(workspace=str(tmp_path), session_id="session-1"),
    )
    assert result.metadata["error"] is True
    assert result.metadata["reason"] == "gateway_unavailable"

    gateway = AgentGateway()
    root_a = gateway.ensure_root("session-a")
    root_b = gateway.ensure_root("session-b")
    ctx = ToolContext(
        workspace=str(tmp_path),
        session_id="session-a",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_a),
    )
    cross_session = await MessageTool().execute(
        {
            "action": "send",
            "target_run_id": root_b,
            "message_type": "message",
            "payload": {},
        },
        ctx,
    )

    assert cross_session.metadata["error"] is True
    assert cross_session.metadata["reason"] == "gateway_error"


@pytest.mark.asyncio
async def test_message_result_sets_child_run_result(tmp_path):
    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-1")
    started = False

    async def runner(run_id: str) -> str:
        nonlocal started
        started = True
        ctx = ToolContext(
            workspace=str(tmp_path),
            session_id="session-1",
            runtime=AgentToolRuntime(subagent_transport=gateway, run_id=run_id),
        )
        await MessageTool().execute(
            {
                "action": "send",
                "message_type": "result",
                "payload": {"result": "explicit child result"},
            },
            ctx,
        )
        return "fallback result"

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="child",
        description="child",
        runner=runner,
    )

    assert run.status == "running"
    run = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=1)
    assert started is True
    assert run.status == "completed"
    assert run.result == {"result": "explicit child result"}
