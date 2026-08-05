import asyncio

import pytest

from voidx.agent.gateway import AgentGateway
from voidx.agent.gateway.gateway import AgentGatewayError


@pytest.mark.asyncio
async def test_root_spawn_send_receive_and_wait_result():
    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-1")
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(run_id: str) -> str:
        await gateway.send(
            sender_run_id=run_id,
            target_run_id=root_id,
            message_type="progress",
            payload={"step": "working"},
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
        ("progress", {"step": "working"}),
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
    gateway = AgentGateway()
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
async def test_result_message_completes_run_and_blocks_later_messages():
    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-1")

    async def runner(run_id: str) -> str:
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
                message_type="progress",
                payload={"step": "after result"},
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
    messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)
    assert [(message.type, message.payload) for message in messages] == [
        ("result", {"result": "first"}),
        ("completed", {"run_id": run.run_id}),
    ]


@pytest.mark.asyncio
async def test_route_rejects_siblings_cross_session_and_unknown_runs():
    gateway = AgentGateway()
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
    gateway = AgentGateway()
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
    assert "boom" in (failed.error or "")
    failed_messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)
    assert [(message.type, message.payload) for message in failed_messages] == [
        ("failed", {"run_id": failed.run_id, "error": "boom"}),
    ]

    await gateway.close_session("session-1")
    assert gateway.list_runs(session_id="session-1") == []



@pytest.mark.asyncio
async def test_runner_return_after_result_does_not_overwrite_payload():
    gateway = AgentGateway()
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
    gateway = AgentGateway()
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
    gateway = AgentGateway(inbox_capacity=1)
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
        message_type="progress",
        payload={"step": "fill"},
    )
    with pytest.raises(AgentGatewayError, match="Inbox is full"):
        await gateway.send(
            sender_run_id=child.run_id,
            target_run_id=root_id,
            message_type="progress",
            payload={"step": "overflow"},
        )

    release.set()
    waited = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=child.run_id,
        timeout=0.2,
    )
    assert waited.status == "completed"
    messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)
    assert any(message.type == "completed" for message in messages)
    assert any(
        message.type == "completed" and message.payload.get("run_id") == child.run_id
        for message in messages
    )


@pytest.mark.asyncio
async def test_payload_size_limit_rejects_oversized_message():
    gateway = AgentGateway(max_payload_bytes=32)
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
    gateway = AgentGateway()
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
    gateway = AgentGateway()
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
    gateway = AgentGateway()
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
    gateway = AgentGateway()
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
    assert timed_out.wait_outcome == "timed_out_still_running"
    await gateway.cancel(requester_run_id=root_id, target_run_id=run.run_id)


@pytest.mark.asyncio
async def test_wait_distinguishes_terminal_transition_from_cached_terminal_result():
    gateway = AgentGateway()
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
