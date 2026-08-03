"""Loop conversational mode — idle turns run in the host session.

When a loop-profile session has no active loop, a user message runs an
in-session loop-profile turn (idle phase). If that turn submits a LoopSpec via
loop(op="init"), the autonomous loop starts; otherwise the session simply
continues.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.domain.loop import LoopSpec
from voidx.agent.domain.thread import AgentThread


class FakeRuntime:
    def __init__(self, spec: dict | None = None):
        self.spec = spec
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        controller = getattr(request.context, "loop_intake_controller", None)
        if self.spec is not None and controller is not None:
            await controller.submit_init(LoopSpec(**self.spec))
        return SimpleNamespace(final_assistant_summary="", stop_signal="")


class FakeLoopService:
    def __init__(self):
        self.started: list[tuple[str | None, object]] = []

    async def start(self, parent_thread_id, spec):
        self.started.append((parent_thread_id, spec))
        return SimpleNamespace(active=True, loop_thread_id="loop:1", prompt_summary=spec.prompt[:80])


def _make_service(runtime, loop_service):
    from voidx.agent.application.loop_idle import LoopIdleTurnService

    return LoopIdleTurnService(runtime, loop_service)


@pytest.mark.asyncio
async def test_idle_turn_runs_in_host_session_with_loop_profile() -> None:
    runtime = FakeRuntime()
    service = _make_service(runtime, FakeLoopService())
    thread = AgentThread(thread_id="host-1", session_id="host-1", workspace="/ws")

    await service.run("just chatting", thread, parent_thread_id="host-1")

    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.thread.thread_id == "host-1"
    assert request.context.runtime_profile.profile_id == "loop"
    assert request.context.loop_phase == "idle"
    assert request.context.loop_intake_controller is not None
    assert request.user_text == "just chatting"
    assert request.persist_user_input is False


@pytest.mark.asyncio
async def test_idle_turn_binds_readonly_tools_plus_loop_and_clarify() -> None:
    runtime = FakeRuntime()
    service = _make_service(runtime, FakeLoopService())
    thread = AgentThread(thread_id="host-1", session_id="host-1")

    await service.run("hi", thread, parent_thread_id="host-1")

    policy = runtime.requests[0].context.tool_policy
    assert policy.allows("loop")
    assert policy.allows("clarify")
    assert policy.allows("read")
    assert not policy.allows("bash")
    assert not policy.allows("write")


@pytest.mark.asyncio
async def test_idle_turn_starts_loop_when_init_submitted() -> None:
    runtime = FakeRuntime({"prompt": "Monitor build status", "interval_seconds": 60})
    loop_service = FakeLoopService()
    service = _make_service(runtime, loop_service)
    thread = AgentThread(thread_id="host-1", session_id="host-1")

    status = await service.run("monitor builds", thread, parent_thread_id="host-1")

    assert status is not None
    assert len(loop_service.started) == 1
    parent, spec = loop_service.started[0]
    assert parent == "host-1"
    assert spec.prompt == "Monitor build status"
    assert spec.interval_seconds == 60


@pytest.mark.asyncio
async def test_idle_turn_returns_none_when_no_loop_started() -> None:
    runtime = FakeRuntime()
    loop_service = FakeLoopService()
    service = _make_service(runtime, loop_service)
    thread = AgentThread(thread_id="host-1", session_id="host-1")

    status = await service.run("what can you do?", thread, parent_thread_id="host-1")

    assert status is None
    assert loop_service.started == []
