import asyncio
from typing import get_args

import pytest
from pydantic import ValidationError

from voidx.agent.adapters.subagent import InProcessSubagentGateway
from voidx.agent.domain.subagent import (
    USER_MESSAGE_TYPES,
    AgentGatewayError,
    AgentMessage,
    AgentMessageType,
    UserMessageType,
)


def test_progress_is_removed_from_transport_protocol():
    assert "progress" not in get_args(UserMessageType)
    assert "progress" not in get_args(AgentMessageType)
    assert "progress" not in USER_MESSAGE_TYPES

    with pytest.raises(ValidationError):
        AgentMessage.model_validate(
            {
                "message_id": "message-1",
                "session_id": "session-1",
                "source_run_id": "run-1",
                "target_run_id": "root:session-1",
                "type": "progress",
                "payload": {"step": "working"},
                "created_at": 1.0,
            }
        )


@pytest.mark.asyncio
async def test_default_inbox_capacity_is_256_for_root_and_child():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-capacity")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "done"

    child = await gateway.spawn(
        session_id="session-capacity",
        parent_run_id=root_id,
        agent_name="child",
        description="capacity",
        runner=runner,
    )

    assert gateway._inbox_capacity == 256
    assert gateway._runs[root_id].inbox.maxsize == 256
    assert gateway._runs[child.run_id].inbox.maxsize == 256

    release.set()
    await gateway.wait(requester_run_id=root_id, target_run_id=child.run_id, timeout=1)


@pytest.mark.asyncio
async def test_gateway_rejects_progress_without_changing_run_state():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-progress")
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        started.set()
        await release.wait()
        return "done"

    child = await gateway.spawn(
        session_id="session-progress",
        parent_run_id=root_id,
        agent_name="child",
        description="reject progress",
        runner=runner,
    )
    await started.wait()

    with pytest.raises(AgentGatewayError):
        await gateway.send(
            sender_run_id=child.run_id,
            target_run_id=root_id,
            message_type="progress",
            payload={"step": "working"},
        )

    assert gateway.lookup_run(child.run_id).status == "running"
    assert gateway.lookup_run(root_id).status == "running"
    assert await gateway.receive(run_id=root_id, limit=1, timeout=0) == []

    release.set()
    await gateway.wait(requester_run_id=root_id, target_run_id=child.run_id, timeout=1)


@pytest.mark.asyncio
async def test_root_spawn_send_receive_and_wait_result():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(run_id: str) -> str:
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="message",
            payload={"text": "working"},
        )
        started.set()
        await release.wait()
        return "child complete"

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="voidx",
        description="do work",
        runner=runner,
    )

    assert run.parent_run_id == root_id
    assert run.agent_type == "sub"
    assert run.status == "running"
    assert run.result is None
    await started.wait()

    messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)
    assert [(message.type, message.payload) for message in messages] == [
        ("message", {"text": "working"}),
    ]
    release.set()

    waited = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=run.run_id,
        timeout=0.1,
    )
    assert waited.status == "completed"
    assert waited.result == {"result": "child complete"}
    terminal_messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)
    assert [(message.type, message.payload) for message in terminal_messages] == [
        ("completed", {"run_id": run.run_id}),
    ]
@pytest.mark.asyncio
async def test_wait_timeout_zero_waits_indefinitely_until_terminal():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "done"

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="voidx",
        description="slow",
        runner=runner,
    )

    async def wait_with_timeout_zero():
        return await gateway.wait(
            requester_run_id=root_id,
            target_run_id=run.run_id,
            timeout=0,
        )

    wait_task = asyncio.create_task(wait_with_timeout_zero())
    await asyncio.sleep(0.05)
    assert not wait_task.done()
    release.set()
    waited = await asyncio.wait_for(wait_task, timeout=1)
    assert waited.status == "completed"
    assert waited.result == {"result": "done"}


@pytest.mark.asyncio
async def test_result_message_completes_run_and_blocks_later_messages(monkeypatch):
    import voidx.agent.adapters.subagent.inprocess_gateway as gateway_module

    now = [100.0]
    monkeypatch.setattr(gateway_module.time, "time", lambda: now[0])
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")

    async def runner(run_id: str) -> str:
        now[0] = 110.0
        gateway.start_model_activity(run_id, activity_id="llm-result")
        now[0] = 120.0
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="result",
            payload={"result": "first"},
        )
        with pytest.raises(AgentGatewayError, match="terminal"):
            await gateway.send(
                sender_run_id=run_id,
                target_run_id=root_id,
                message_type="result",
                payload={"result": "second"},
            )
        with pytest.raises(AgentGatewayError, match="terminal"):
            await gateway.send(
                sender_run_id=run_id,
                target_run_id=root_id,
                message_type="message",
                payload={"text": "after result"},
            )
        return "ignored"

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="voidx",
        description="result once",
        runner=runner,
    )

    assert run.status == "running"
    waited = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=0.1)
    assert waited.status == "completed"
    assert waited.result == {"result": "first"}
    assert waited.current_activity is None
    assert waited.last_activity_at == 120.0
    assert waited.updated_at == 120.0
    assert gateway._runs[run.run_id].active_activities == {}
    messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)
    assert [(message.type, message.payload) for message in messages] == [
        ("result", {"result": "first"}),
        ("completed", {"run_id": run.run_id}),
    ]


@pytest.mark.asyncio
async def test_route_rejects_siblings_cross_session_and_unknown_runs():
    gateway = InProcessSubagentGateway()
    root_a = gateway.ensure_root("session-a")
    root_b = gateway.ensure_root("session-b")

    async def runner(_run_id: str) -> str:
        return "ok"

    child_a = await gateway.spawn(
        session_id="session-a",
        parent_run_id=root_a,
        agent_name="child-a",
        description="a",
        runner=runner,
    )
    sibling_a = await gateway.spawn(
        session_id="session-a",
        parent_run_id=root_a,
        agent_name="sibling-a",
        description="sibling",
        runner=runner,
    )
    child_b = await gateway.spawn(
        session_id="session-b",
        parent_run_id=root_b,
        agent_name="child-b",
        description="b",
        runner=runner,
    )

    with pytest.raises(AgentGatewayError, match="not allowed"):
        await gateway.send(
            sender_run_id=child_a.run_id,
            target_run_id=sibling_a.run_id,
            message_type="message",
            payload={},
        )

    with pytest.raises(AgentGatewayError, match="same session"):
        await gateway.send(
            sender_run_id=root_a,
            target_run_id=child_b.run_id,
            message_type="message",
            payload={},
        )

    with pytest.raises(AgentGatewayError, match="Unknown run"):
        await gateway.receive(run_id="missing", limit=1, timeout=0)


@pytest.mark.asyncio
async def test_wait_timeout_failure_cancellation_and_close_session_cleanup():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    started = asyncio.Event()

    async def long_runner(_run_id: str) -> str:
        started.set()
        await asyncio.sleep(10)
        return "never"

    pending_task = asyncio.create_task(
        gateway.spawn(
            session_id="session-1",
            parent_run_id=root_id,
            agent_name="slow",
            description="slow",
            runner=long_runner,
        )
    )
    await started.wait()
    pending_runs = [
        run for run in gateway.list_runs(session_id="session-1") if run.agent_type == "sub"
    ]
    assert len(pending_runs) == 1
    pending_run = pending_runs[0]

    timed_out = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=pending_run.run_id,
        timeout=0.01,
    )
    assert timed_out.status == "running"

    cancelled = await gateway.cancel(
        requester_run_id=root_id,
        target_run_id=pending_run.run_id,
    )
    assert cancelled.status == "cancelled"
    assert cancelled.current_activity is None
    assert cancelled.active_tools == []
    assert cancelled.last_activity_at == cancelled.updated_at
    assert (await pending_task).status == "running"
    cancel_messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)
    assert [(message.type, message.payload) for message in cancel_messages] == [
        ("cancelled", {"run_id": pending_run.run_id}),
    ]

    async def failing_runner(_run_id: str) -> str:
        raise RuntimeError("boom")

    failed = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="bad",
        description="bad",
        runner=failing_runner,
    )
    assert failed.status == "running"
    failed = await gateway.wait(requester_run_id=root_id, target_run_id=failed.run_id, timeout=0.1)
    assert failed.status == "failed"
    assert failed.current_activity is None
    assert failed.active_tools == []
    assert failed.last_activity_at == failed.updated_at
    assert "boom" in (failed.error or "")
    failed_messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)
    assert [(message.type, message.payload) for message in failed_messages] == [
        ("failed", {"run_id": failed.run_id, "error": "boom"}),
    ]

    await gateway.close_session("session-1")
    assert gateway.list_runs(session_id="session-1") == []



@pytest.mark.asyncio
async def test_runner_return_after_result_does_not_overwrite_payload():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    finished = asyncio.Event()

    async def runner(run_id: str) -> str:
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="result",
            payload={"result": "explicit", "verdict": "PASS"},
        )
        finished.set()
        return "should-not-overwrite"

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="voidx",
        description="preserve result",
        runner=runner,
    )
    await finished.wait()
    waited = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=0.2)
    assert waited.status == "completed"
    assert waited.result == {"result": "explicit", "verdict": "PASS"}


@pytest.mark.asyncio
async def test_get_parent_run_id_and_close_all():
    gateway = InProcessSubagentGateway()
    root_a = gateway.ensure_root("session-a")
    root_b = gateway.ensure_root("session-b")

    async def runner(_run_id: str) -> str:
        await asyncio.sleep(10)
        return "late"

    child = await gateway.spawn(
        session_id="session-a",
        parent_run_id=root_a,
        agent_name="child",
        description="child",
        runner=runner,
    )
    assert gateway.get_parent_run_id(child.run_id) == root_a
    assert gateway.get_parent_run_id(root_a) is None
    assert gateway.get_parent_run_id("missing") is None

    await gateway.spawn(
        session_id="session-b",
        parent_run_id=root_b,
        agent_name="other",
        description="other",
        runner=runner,
    )
    await gateway.close_all()
    assert gateway.list_runs() == []



@pytest.mark.asyncio
async def test_inbox_full_rejects_regular_message_but_keeps_lifecycle():
    gateway = InProcessSubagentGateway(inbox_capacity=1)
    root_id = gateway.ensure_root("session-1")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "done"

    child = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="child",
        description="child",
        runner=runner,
    )

    await gateway.send(
        sender_run_id=child.run_id,
        target_run_id=root_id,
        message_type="message",
        payload={"text": "fill"},
    )
    with pytest.raises(AgentGatewayError, match="Inbox is full"):
        await gateway.send(
            sender_run_id=child.run_id,
            target_run_id=root_id,
            message_type="message",
            payload={"text": "overflow"},
        )

    release.set()
    waited = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=child.run_id,
        timeout=0.2,
    )
    assert waited.status == "completed"
    messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)
    assert [(message.type, message.payload) for message in messages] == [
        ("completed", {"run_id": child.run_id}),
    ]



@pytest.mark.asyncio
async def test_result_completes_child_when_capacity_one_inbox_is_full():
    gateway = InProcessSubagentGateway(inbox_capacity=1)
    root_id = gateway.ensure_root("session-result-full")
    filled = asyncio.Event()
    release = asyncio.Event()
    payload = {"result": "authoritative", "verdict": "PASS"}

    async def runner(run_id: str) -> str:
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="message",
            payload={"text": "fill"},
        )
        filled.set()
        await release.wait()
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="result",
            payload=payload,
        )
        return "must-not-overwrite"

    child = await gateway.spawn(
        session_id="session-result-full",
        parent_run_id=root_id,
        agent_name="child",
        description="reliable result",
        runner=runner,
    )
    await filled.wait()
    release.set()

    waited = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=child.run_id,
        timeout=1,
    )

    assert waited.status == "completed"
    assert waited.result == payload
    messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)
    assert [(message.type, message.payload) for message in messages] == [
        ("completed", {"run_id": child.run_id}),
    ]


@pytest.mark.asyncio
async def test_full_inbox_retains_result_and_completed_notifications_when_capacity_allows_both():
    gateway = InProcessSubagentGateway(inbox_capacity=2)
    root_id = gateway.ensure_root("session-terminal-priority")
    filled = asyncio.Event()
    release = asyncio.Event()

    async def runner(run_id: str) -> str:
        for sequence in range(2):
            await gateway.send(
                sender_run_id=run_id,
                target_run_id=root_id,
                message_type="message",
                payload={"sequence": sequence},
            )
        filled.set()
        await release.wait()
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="result",
            payload={"result": "done"},
        )
        return "ignored"

    child = await gateway.spawn(
        session_id="session-terminal-priority",
        parent_run_id=root_id,
        agent_name="child",
        description="terminal priority",
        runner=runner,
    )
    await filled.wait()
    release.set()

    waited = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=child.run_id,
        timeout=1,
    )
    messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)

    assert waited.status == "completed"
    assert waited.result == {"result": "done"}
    assert [(message.type, message.payload) for message in messages] == [
        ("result", {"result": "done"}),
        ("completed", {"run_id": child.run_id}),
    ]


@pytest.mark.asyncio
async def test_51_results_complete_without_parent_receiving_from_default_inbox():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-51-results")

    async def runner(run_id: str) -> str:
        payload = {"result": run_id}
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="result",
            payload=payload,
        )
        return "ignored"

    children = [
        await gateway.spawn(
            session_id="session-51-results",
            parent_run_id=root_id,
            agent_name=f"child-{index}",
            description="result pressure",
            runner=runner,
        )
        for index in range(51)
    ]
    waited = await asyncio.gather(
        *(
            gateway.wait(
                requester_run_id=root_id,
                target_run_id=child.run_id,
                timeout=1,
            )
            for child in children
        )
    )

    assert all(run.status == "completed" for run in waited)
    assert [run.result for run in waited] == [
        {"result": child.run_id} for child in children
    ]


@pytest.mark.asyncio
async def test_128_results_remain_authoritative_while_notifications_are_evicted():
    gateway = InProcessSubagentGateway(inbox_capacity=17)
    root_id = gateway.ensure_root("session-128-results")

    async def runner(run_id: str) -> str:
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="result",
            payload={"result": run_id, "complete": True},
        )
        return "ignored"

    children = [
        await gateway.spawn(
            session_id="session-128-results",
            parent_run_id=root_id,
            agent_name=f"child-{index}",
            description="concurrent terminal pressure",
            runner=runner,
        )
        for index in range(128)
    ]
    waited = await asyncio.gather(
        *(
            gateway.wait(
                requester_run_id=root_id,
                target_run_id=child.run_id,
                timeout=2,
            )
            for child in children
        )
    )
    notifications = await gateway.receive(run_id=root_id, limit=256, timeout=0)

    assert all(run.status == "completed" for run in waited)
    assert [run.result for run in waited] == [
        {"result": child.run_id, "complete": True} for child in children
    ]
    assert len(notifications) == 17
    assert all(message.type in {"result", "completed"} for message in notifications)

@pytest.mark.asyncio
async def test_payload_size_limit_rejects_oversized_message():
    gateway = InProcessSubagentGateway(max_payload_bytes=32)
    root_id = gateway.ensure_root("session-1")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "ok"

    child = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="child",
        description="child",
        runner=runner,
    )

    with pytest.raises(AgentGatewayError, match="Payload is too large"):
        await gateway.send(
            sender_run_id=child.run_id,
            target_run_id=root_id,
            message_type="message",
            payload={"blob": "x" * 64},
        )

    release.set()
    await gateway.wait(requester_run_id=root_id, target_run_id=child.run_id, timeout=0.2)


@pytest.mark.asyncio
async def test_route_rejects_grandparent_grandchild_send_and_control():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    hold = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await hold.wait()
        return "ok"

    child = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="child",
        description="child",
        runner=runner,
    )
    grandchild = await gateway.spawn(
        session_id="session-1",
        parent_run_id=child.run_id,
        agent_name="grandchild",
        description="grandchild",
        runner=runner,
    )

    with pytest.raises(AgentGatewayError, match="not allowed"):
        await gateway.send(
            sender_run_id=root_id,
            target_run_id=grandchild.run_id,
            message_type="message",
            payload={"text": "no direct"},
        )
    with pytest.raises(AgentGatewayError, match="not allowed"):
        await gateway.send(
            sender_run_id=grandchild.run_id,
            target_run_id=root_id,
            message_type="message",
            payload={"text": "no skip"},
        )
    with pytest.raises(AgentGatewayError, match="not allowed"):
        await gateway.wait(
            requester_run_id=root_id,
            target_run_id=grandchild.run_id,
            timeout=0.01,
        )
    with pytest.raises(AgentGatewayError, match="not allowed"):
        await gateway.cancel(
            requester_run_id=root_id,
            target_run_id=grandchild.run_id,
        )
    # control is root->direct-child only; non-root parents cannot wait/cancel
    with pytest.raises(AgentGatewayError, match="not allowed"):
        await gateway.wait(
            requester_run_id=child.run_id,
            target_run_id=grandchild.run_id,
            timeout=0.01,
        )

    hold.set()
    await gateway.close_session("session-1")


@pytest.mark.asyncio
async def test_close_session_makes_old_runs_inaccessible():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    started = asyncio.Event()

    async def runner(_run_id: str) -> str:
        started.set()
        await asyncio.sleep(10)
        return "late"

    child = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="child",
        description="child",
        runner=runner,
    )
    await started.wait()
    await gateway.close_session("session-1")

    assert gateway.list_runs(session_id="session-1") == []
    with pytest.raises(AgentGatewayError, match="Unknown run"):
        await gateway.receive(run_id=root_id, limit=1, timeout=0)
    with pytest.raises(AgentGatewayError, match="Unknown run"):
        await gateway.receive(run_id=child.run_id, limit=1, timeout=0)
    with pytest.raises(AgentGatewayError, match="Unknown run"):
        gateway.get_run(requester_run_id=root_id, target_run_id=child.run_id)


@pytest.mark.asyncio
async def test_send_rejects_lifecycle_message_types():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        started.set()
        await release.wait()
        return "ok"

    child = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="child",
        description="child",
        runner=runner,
    )
    await started.wait()

    for lifecycle_type in ("completed", "failed", "cancelled"):
        with pytest.raises(AgentGatewayError, match="[Ll]ifecycle"):
            await gateway.send(
                sender_run_id=child.run_id,
                target_run_id=root_id,
                message_type=lifecycle_type,
                payload={},
            )

    assert gateway.get_run(requester_run_id=root_id, target_run_id=child.run_id).status == "running"
    release.set()
    await gateway.wait(requester_run_id=root_id, target_run_id=child.run_id, timeout=5)


@pytest.mark.asyncio
async def test_wait_marks_timeout_while_run_is_still_active():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-wait-outcome")
    started = asyncio.Event()

    async def runner(_run_id: str) -> str:
        started.set()
        await asyncio.sleep(10)
        return "late"

    run = await gateway.spawn(
        session_id="session-wait-outcome",
        parent_run_id=root_id,
        agent_name="slow",
        description="slow",
        runner=runner,
    )
    await started.wait()

    timed_out = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=run.run_id,
        timeout=0.01,
    )

    assert timed_out.status == "running"
    assert timed_out.wait_outcome == "timed_out"
    await gateway.cancel(requester_run_id=root_id, target_run_id=run.run_id)


@pytest.mark.asyncio
async def test_wait_distinguishes_terminal_transition_from_cached_terminal_result():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-wait-terminal")

    async def runner(_run_id: str) -> str:
        return "done"

    run = await gateway.spawn(
        session_id="session-wait-terminal",
        parent_run_id=root_id,
        agent_name="fast",
        description="fast",
        runner=runner,
    )
    first = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=run.run_id,
        timeout=1,
    )
    second = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=run.run_id,
        timeout=1,
    )

    assert first.status == "completed"
    assert first.wait_outcome == "terminal_reached_during_wait"
    assert second.status == "completed"
    assert second.wait_outcome == "already_terminal"
    assert second.result == first.result


@pytest.mark.asyncio
async def test_unknown_run_exposes_stable_reason():
    gateway = InProcessSubagentGateway()

    with pytest.raises(AgentGatewayError, match="Unknown run") as error:
        await gateway.receive(run_id="missing", limit=1, timeout=0)

    assert error.value.reason == "unknown_run"


@pytest.mark.asyncio
async def test_cancel_times_out_when_child_suppresses_cancellation(monkeypatch):
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()
        return "eventually done"

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="stubborn",
        description="suppress cancellation",
        runner=runner,
    )
    await started.wait()
    monkeypatch.setattr(
        "voidx.agent.adapters.subagent.inprocess_gateway._CANCEL_ACK_TIMEOUT",
        0.01,
    )

    try:
        with pytest.raises(
            AgentGatewayError,
            match="Child cancellation was not acknowledged",
        ) as error:
            await gateway.cancel(requester_run_id=root_id, target_run_id=run.run_id)

        assert error.value.reason == "cancel_timeout"
        current = gateway.lookup_run(run.run_id)
        assert current is not None
        assert current.status == "running"
    finally:
        release.set()
        await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=1)


@pytest.mark.asyncio
async def test_cancel_returns_cancelled_and_preserves_already_terminal_run():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    started = asyncio.Event()

    async def runner(_run_id: str) -> str:
        started.set()
        await asyncio.Future()

    running = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="running",
        description="cancel normally",
        runner=runner,
    )
    await started.wait()

    cancelled = await gateway.cancel(requester_run_id=root_id, target_run_id=running.run_id)
    cancelled_again = await gateway.cancel(
        requester_run_id=root_id,
        target_run_id=running.run_id,
    )

    assert cancelled.status == "cancelled"
    assert cancelled_again == cancelled


@pytest.mark.asyncio
async def test_cancel_reaps_runner_after_result_without_overwriting_terminal_result():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-result-cancel")
    result_sent = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def runner(run_id: str) -> str:
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="result",
            payload={"result": "authoritative"},
        )
        result_sent.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    run = await gateway.spawn(
        session_id="session-result-cancel",
        parent_run_id=root_id,
        agent_name="child",
        description="result then hang",
        runner=runner,
    )
    await result_sent.wait()

    try:
        completed = await gateway.cancel(
            requester_run_id=root_id,
            target_run_id=run.run_id,
        )

        assert completed.status == "completed"
        assert completed.result == {"result": "authoritative"}
        assert task_cancelled.is_set()
        assert gateway._runs[run.run_id].task.done()
    finally:
        task = gateway._runs[run.run_id].task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await gateway.close_session("session-result-cancel")


@pytest.mark.asyncio
async def test_close_session_reaps_runner_after_result_without_hanging():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-result-close")
    result_sent = asyncio.Event()

    async def runner(run_id: str) -> str:
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="result",
            payload={"result": "done"},
        )
        result_sent.set()
        await asyncio.Future()

    run = await gateway.spawn(
        session_id="session-result-close",
        parent_run_id=root_id,
        agent_name="child",
        description="result then hang",
        runner=runner,
    )
    await result_sent.wait()
    waited = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=run.run_id,
        timeout=0.1,
    )
    assert waited.status == "completed"

    close_task = asyncio.create_task(gateway.close_session("session-result-close"))
    try:
        done, _pending = await asyncio.wait({close_task}, timeout=0.1)
        assert close_task in done
        await close_task
        assert gateway.list_runs(session_id="session-result-close") == []
    finally:
        if not close_task.done():
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
        if gateway.lookup_run(run.run_id) is not None:
            await gateway.close_session("session-result-close")


@pytest.mark.asyncio
async def test_close_all_reaps_terminal_runner_tasks_without_hanging():
    gateway = InProcessSubagentGateway()
    roots = [gateway.ensure_root(f"session-result-close-{index}") for index in range(2)]
    result_sent = [asyncio.Event(), asyncio.Event()]

    async def runner(index: int, run_id: str) -> str:
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=roots[index],
            message_type="result",
            payload={"result": f"done-{index}"},
        )
        result_sent[index].set()
        await asyncio.Future()

    children = []
    for index, root_id in enumerate(roots):
        children.append(await gateway.spawn(
            session_id=f"session-result-close-{index}",
            parent_run_id=root_id,
            agent_name="child",
            description="result then hang",
            runner=lambda run_id, index=index: runner(index, run_id),
        ))
    await asyncio.gather(*(event.wait() for event in result_sent))

    close_task = asyncio.create_task(gateway.close_all())
    try:
        done, _pending = await asyncio.wait({close_task}, timeout=0.1)
        assert close_task in done
        await close_task
        assert gateway.list_runs() == []
    finally:
        if not close_task.done():
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
        tasks = [
            gateway._runs[child.run_id].task
            for child in children
            if child.run_id in gateway._runs
        ]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        if gateway.list_runs():
            await gateway.close_all()


@pytest.mark.asyncio
async def test_close_session_times_out_when_terminal_runner_suppresses_cancellation(monkeypatch):
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-stubborn-result")
    result_sent = asyncio.Event()
    release = asyncio.Event()

    async def runner(run_id: str) -> str:
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="result",
            payload={"result": "authoritative"},
        )
        result_sent.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()
        return "ignored"

    run = await gateway.spawn(
        session_id="session-stubborn-result",
        parent_run_id=root_id,
        agent_name="child",
        description="suppress cancellation after result",
        runner=runner,
    )
    await result_sent.wait()
    monkeypatch.setattr(
        "voidx.agent.adapters.subagent.inprocess_gateway._CANCEL_ACK_TIMEOUT",
        0.01,
    )

    close_task = asyncio.create_task(gateway.close_session("session-stubborn-result"))
    try:
        done, _pending = await asyncio.wait({close_task}, timeout=0.1)

        assert close_task in done
        with pytest.raises(
            AgentGatewayError,
            match="Child cancellation was not acknowledged",
        ) as error:
            await close_task
        assert error.value.reason == "cancel_timeout"
        current = gateway.lookup_run(run.run_id)
        assert current is not None
        assert current.status == "completed"
        assert current.result == {"result": "authoritative"}
    finally:
        release.set()
        if not close_task.done():
            await close_task
        task = gateway._runs.get(run.run_id).task if run.run_id in gateway._runs else None
        if task is not None and not task.done():
            await asyncio.wait_for(task, timeout=1)
        if gateway.lookup_run(run.run_id) is not None:
            await gateway.close_session("session-stubborn-result")


@pytest.mark.asyncio
async def test_send_validation_precedence_is_route_then_open_then_payload():
    gateway = InProcessSubagentGateway(max_payload_bytes=10)
    root_id = gateway.ensure_root("session-validation-order")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "done"

    first = await gateway.spawn(
        session_id="session-validation-order",
        parent_run_id=root_id,
        agent_name="first",
        description="first",
        runner=runner,
    )
    sibling = await gateway.spawn(
        session_id="session-validation-order",
        parent_run_id=root_id,
        agent_name="sibling",
        description="sibling",
        runner=runner,
    )

    try:
        with pytest.raises(AgentGatewayError, match="Route not allowed"):
            await gateway.send(
                sender_run_id=first.run_id,
                target_run_id=sibling.run_id,
                message_type="message",
                payload={"text": "payload is much too large"},
            )

        await gateway.cancel(requester_run_id=root_id, target_run_id=first.run_id)
        with pytest.raises(AgentGatewayError, match="Source run is terminal"):
            await gateway.send(
                sender_run_id=first.run_id,
                target_run_id=root_id,
                message_type="message",
                payload={"text": "payload is much too large"},
            )
    finally:
        release.set()
        await gateway.close_session("session-validation-order")


@pytest.mark.asyncio
@pytest.mark.parametrize("close_action", ["session", "all"])
async def test_runner_cannot_reap_its_own_task(close_action):
    gateway = InProcessSubagentGateway()
    session_id = f"session-self-reap-{close_action}"
    root_id = gateway.ensure_root(session_id)
    rejection: dict[str, str] = {}

    async def runner(_run_id: str) -> str:
        try:
            if close_action == "session":
                await gateway.close_session(session_id)
            else:
                await gateway.close_all()
        except AgentGatewayError as error:
            rejection["reason"] = error.reason
            return "self reap rejected"
        return "unexpected success"

    run = await gateway.spawn(
        session_id=session_id,
        parent_run_id=root_id,
        agent_name="child",
        description="self reap",
        runner=runner,
    )

    waited = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=run.run_id,
        timeout=1,
    )

    assert waited.status == "completed"
    assert waited.result == {"result": "self reap rejected"}
    assert rejection == {"reason": "self_reap"}
    assert gateway.lookup_run(root_id) is not None
    assert gateway.lookup_run(run.run_id) is not None
    await gateway.close_session(session_id)


@pytest.mark.asyncio
async def test_child_self_cancel_is_rejected_by_control_route():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-self-cancel")
    rejection: dict[str, str] = {}

    async def runner(run_id: str) -> str:
        try:
            await gateway.cancel(
                requester_run_id=run_id,
                target_run_id=run_id,
            )
        except AgentGatewayError as error:
            rejection["reason"] = error.reason
            return "self cancel rejected"
        return "unexpected success"

    run = await gateway.spawn(
        session_id="session-self-cancel",
        parent_run_id=root_id,
        agent_name="child",
        description="self cancel",
        runner=runner,
    )
    waited = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=run.run_id,
        timeout=1,
    )

    assert waited.status == "completed"
    assert rejection == {"reason": "route_not_allowed"}
    await gateway.close_session("session-self-cancel")


@pytest.mark.asyncio
async def test_list_child_runs_returns_only_direct_children():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-children")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "done"

    child = await gateway.spawn(
        session_id="session-children",
        parent_run_id=root_id,
        agent_name="child",
        description="direct child",
        runner=runner,
    )
    grandchild = await gateway.spawn(
        session_id="session-children",
        parent_run_id=child.run_id,
        agent_name="grandchild",
        description="nested child",
        runner=runner,
    )

    assert [run.run_id for run in gateway.list_child_runs(root_id)] == [child.run_id]
    assert [run.run_id for run in gateway.list_child_runs(child.run_id)] == [grandchild.run_id]

    release.set()
    await gateway.wait(requester_run_id=root_id, target_run_id=child.run_id, timeout=1)


@pytest.mark.asyncio
async def test_tool_activity_tracks_parallel_calls_and_latest_completion(monkeypatch):
    import voidx.agent.adapters.subagent.inprocess_gateway as gateway_module

    now = [100.0]
    monkeypatch.setattr(gateway_module.time, "time", lambda: now[0])
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-activity")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "done"

    child = await gateway.spawn(
        session_id="session-activity",
        parent_run_id=root_id,
        agent_name="child",
        description="track tools",
        runner=runner,
    )

    now[0] = 110.0
    gateway.start_tool_activity(child.run_id, tool_name="read", tool_call_id="call-read")
    now[0] = 111.0
    gateway.start_tool_activity(child.run_id, tool_name="search", tool_call_id="call-search")

    active = gateway.lookup_run(child.run_id)
    assert active is not None
    assert [(item.tool_name, item.status) for item in active.active_tools] == [
        ("read", "running"),
        ("search", "running"),
    ]
    assert active.updated_at == 111.0

    now[0] = 115.0
    gateway.finish_tool_activity(child.run_id, tool_call_id="call-search", succeeded=False)
    after_failure = gateway.lookup_run(child.run_id)
    assert after_failure is not None
    assert [item.tool_call_id for item in after_failure.active_tools] == ["call-read"]
    assert after_failure.last_tool is not None
    assert after_failure.last_tool.tool_name == "search"
    assert after_failure.last_tool.status == "failed"
    assert after_failure.last_tool.finished_at == 115.0

    now[0] = 120.0
    gateway.finish_tool_activity(child.run_id, tool_call_id="call-read", succeeded=True)
    completed = gateway.lookup_run(child.run_id)
    assert completed is not None
    assert completed.active_tools == []
    assert completed.last_tool is not None
    assert completed.last_tool.tool_name == "read"
    assert completed.last_tool.status == "succeeded"
    assert completed.updated_at == 120.0

    release.set()
    await gateway.wait(requester_run_id=root_id, target_run_id=child.run_id, timeout=1)


@pytest.mark.asyncio
async def test_activity_summary_tracks_abstract_progress_and_unique_files(monkeypatch):
    import voidx.agent.adapters.subagent.inprocess_gateway as gateway_module

    now = [100.0]
    monkeypatch.setattr(gateway_module.time, "time", lambda: now[0])
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-progress-summary")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "done"

    child = await gateway.spawn(
        session_id="session-progress-summary",
        parent_run_id=root_id,
        agent_name="child",
        description="track abstract progress",
        runner=runner,
    )

    calls = [
        ("read", "read-1", {"file_path": "src/a.py"}, True),
        ("read", "read-2", {"file_path": "./src/a.py"}, True),
        ("write", "write-1", {"file_path": "src/a.py"}, True),
        ("replace", "replace-1", {"file_path": "src/a.py"}, False),
        ("manage", "manage-1", {"kind": "file", "op": "move", "moves": [{"src": "src/a.py", "dest": "src/b.py"}]}, True),
        ("bash", "bash-1", {"command": "echo one && echo two"}, False),
        ("search", "search-1", {"query": "needle"}, True),
        ("mcp", "other-1", {"op": "call"}, True),
    ]
    for index, (tool_name, call_id, args, succeeded) in enumerate(calls, start=1):
        now[0] = 100.0 + index
        gateway.start_tool_activity(
            child.run_id,
            tool_name=tool_name,
            tool_call_id=call_id,
            args=args,
            workspace="/workspace",
        )
        gateway.start_tool_activity(
            child.run_id,
            tool_name=tool_name,
            tool_call_id=call_id,
            args=args,
            workspace="/workspace",
        )
        now[0] += 0.5
        gateway.finish_tool_activity(child.run_id, tool_call_id=call_id, succeeded=succeeded)
        gateway.finish_tool_activity(child.run_id, tool_call_id=call_id, succeeded=succeeded)

    run = gateway.lookup_run(child.run_id)
    assert run is not None
    assert run.progress.model_dump() == {
        "files_read": 1,
        "files_edited": 2,
        "commands_run": 1,
        "searches": 1,
        "other_actions": 1,
    }
    assert run.current_activity is not None
    assert run.current_activity.category == "other"
    assert run.recent_activity is not None
    assert run.recent_activity.category == "other"
    assert run.recent_activity.status == "succeeded"
    assert run.last_activity_at == 108.5

    release.set()
    await gateway.wait(requester_run_id=root_id, target_run_id=child.run_id, timeout=1)


@pytest.mark.asyncio
async def test_model_activity_touch_updates_current_and_yields_to_newer_tool(monkeypatch):
    import voidx.agent.adapters.subagent.inprocess_gateway as gateway_module

    now = [200.0]
    monkeypatch.setattr(gateway_module.time, "time", lambda: now[0])
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-model-activity")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "done"

    child = await gateway.spawn(
        session_id="session-model-activity",
        parent_run_id=root_id,
        agent_name="child",
        description="track model activity",
        runner=runner,
    )

    now[0] = 210.0
    gateway.start_model_activity(child.run_id, activity_id="llm-1")
    now[0] = 215.0
    gateway.touch_model_activity(child.run_id, activity_id="llm-1")
    thinking = gateway.lookup_run(child.run_id)
    assert thinking is not None
    assert thinking.current_activity is not None
    assert thinking.current_activity.category == "thinking"
    assert thinking.last_activity_at == 215.0

    now[0] = 216.0
    gateway.start_tool_activity(
        child.run_id,
        tool_name="search",
        tool_call_id="search-1",
        args={"query": "needle"},
        workspace="/workspace",
    )
    searching = gateway.lookup_run(child.run_id)
    assert searching is not None
    assert searching.current_activity is not None
    assert searching.current_activity.category == "searching"

    now[0] = 217.0
    gateway.touch_model_activity(child.run_id, activity_id="llm-1")
    thinking_again = gateway.lookup_run(child.run_id)
    assert thinking_again is not None
    assert thinking_again.current_activity is not None
    assert thinking_again.current_activity.category == "thinking"

    now[0] = 218.0
    gateway.finish_model_activity(child.run_id, activity_id="llm-1", succeeded=False)
    after_model = gateway.lookup_run(child.run_id)
    assert after_model is not None
    assert after_model.current_activity is not None
    assert after_model.current_activity.category == "searching"
    assert after_model.recent_activity is not None
    assert after_model.recent_activity.category == "thinking"
    assert after_model.recent_activity.status == "failed"

    now[0] = 219.0
    gateway.finish_tool_activity(child.run_id, tool_call_id="search-1", succeeded=True)
    after_tool = gateway.lookup_run(child.run_id)
    assert after_tool is not None
    assert after_tool.current_activity is not None
    assert after_tool.current_activity.category == "other"
    assert after_tool.recent_activity is not None
    assert after_tool.recent_activity.category == "searching"
    assert after_tool.last_activity_at == 219.0

    release.set()
    await gateway.wait(requester_run_id=root_id, target_run_id=child.run_id, timeout=1)
