"""Goal conversational mode — idle turns run in the host session.

Covers the session-front-door behavior: when a goal-profile session has no
active goal, a user message runs an in-session goal-profile turn (idle phase).
If that turn submits a GoalSpec via goal(op="init"), the autonomous goal loop
starts; otherwise the session simply continues.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.domain.automation.goal import GoalSpec
from voidx.agent.domain.thread import AgentThread


class FakeRuntime:
    """Records the turn request; optionally submits a GoalSpec during the turn."""

    def __init__(self, spec: dict | None = None):
        self.spec = spec
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        controller = getattr(request.context, "goal_intake_controller", None)
        if self.spec is not None and controller is not None:
            await controller.submit_init(GoalSpec(**self.spec))
        return SimpleNamespace(final_assistant_summary="", stop_signal="")


class FakeGoalService:
    def __init__(self):
        self.started: list[tuple[str | None, object]] = []

    async def start(self, parent_thread_id, spec):
        self.started.append((parent_thread_id, spec))
        return SimpleNamespace(active=True)


def _make_service(runtime, goal_service):
    from voidx.agent.application.automation.goal.goal_idle import GoalIdleTurnService

    return GoalIdleTurnService(runtime, goal_service)


@pytest.mark.asyncio
async def test_idle_turn_runs_in_host_session_with_goal_profile() -> None:
    runtime = FakeRuntime()
    service = _make_service(runtime, FakeGoalService())
    thread = AgentThread(thread_id="host-1", session_id="host-1", workspace="/ws")

    await service.run("just chatting", thread, parent_thread_id="host-1")

    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.thread.thread_id == "host-1"
    assert request.context.runtime_profile.profile_id == "goal"
    assert request.context.goal_phase == "idle"
    assert request.context.goal_intake_controller is not None
    assert request.user_text == "just chatting"
    assert request.display_text == "just chatting"
    # idle turn does not persist user input itself; the caller (agent_service)
    # persists the first message via _persist_first_message to avoid double writes.
    assert request.persist_user_input is False


@pytest.mark.asyncio
async def test_idle_turn_binds_readonly_tools_plus_goal_and_clarify() -> None:
    runtime = FakeRuntime()
    service = _make_service(runtime, FakeGoalService())
    thread = AgentThread(thread_id="host-1", session_id="host-1")

    await service.run("hi", thread, parent_thread_id="host-1")

    policy = runtime.requests[0].context.tool_policy
    assert policy.allows("goal")
    assert policy.allows("clarify")
    assert policy.allows("read")
    assert not policy.allows("bash")
    assert not policy.allows("write")


@pytest.mark.asyncio
async def test_idle_turn_starts_goal_when_init_submitted() -> None:
    runtime = FakeRuntime({"objective": "fix flaky tests", "acceptance_condition": "suite green"})
    goal_service = FakeGoalService()
    service = _make_service(runtime, goal_service)
    thread = AgentThread(thread_id="host-1", session_id="host-1")

    status = await service.run("make the tests reliable", thread, parent_thread_id="host-1")

    assert status is not None
    assert len(goal_service.started) == 1
    parent, spec = goal_service.started[0]
    assert parent == "host-1"
    assert spec.objective == "fix flaky tests"


@pytest.mark.asyncio
async def test_idle_turn_returns_none_when_no_goal_started() -> None:
    runtime = FakeRuntime()  # no spec submitted -> pure conversation
    goal_service = FakeGoalService()
    service = _make_service(runtime, goal_service)
    thread = AgentThread(thread_id="host-1", session_id="host-1")

    status = await service.run("what can you do?", thread, parent_thread_id="host-1")

    assert status is None
    assert goal_service.started == []
