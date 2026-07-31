"""First-message mode dispatch: goal/loop profiles start on the first message."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.application.agent_service import AgentService


class FakeLoopService:
    def __init__(self):
        self.started: list[tuple[str | None, object]] = []

    async def start(self, parent_thread_id, spec):
        self.started.append((parent_thread_id, spec))
        return SimpleNamespace(active=True, loop_thread_id="loop:1")


class FakeGoalService:
    def __init__(self):
        self.started: list[tuple[str | None, object]] = []

    async def start(self, parent_thread_id, spec):
        self.started.append((parent_thread_id, spec))
        return SimpleNamespace(active=True, objective_summary=spec.objective, attempt_count=0, max_attempts=1)


class FakeIntakeRuntime:
    def __init__(self, summary: str = ""):
        self.summary = summary
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        return SimpleNamespace(final_assistant_summary=self.summary)


def _service(profile: str, **overrides):
    execution = SimpleNamespace(
        session_id="host-session",
        session=SimpleNamespace(id="host-session", runtime_profile=profile, message_count=0),
        loop_service=FakeLoopService(),
        goal_service=FakeGoalService(),
    )
    return AgentService(execution, runtime=FakeIntakeRuntime(), **overrides)


@pytest.mark.asyncio
async def test_loop_profile_first_message_starts_dynamic_loop() -> None:
    from voidx.agent.domain.loop import LoopSpec

    service = _service("loop")

    handled = await service._handle_loop_first_message("fix the flaky test", thread_id="")

    assert handled is True
    assert len(service._execution.loop_service.started) == 1
    parent, spec = service._execution.loop_service.started[0]
    assert parent == "host-session"
    assert isinstance(spec, LoopSpec)
    assert spec.prompt == "fix the flaky test"
    assert spec.interval_seconds is None


@pytest.mark.asyncio
async def test_loop_first_message_without_service_falls_through() -> None:
    service = _service("loop")
    service._execution.loop_service = None

    handled = await service._handle_loop_first_message("hello", thread_id="")

    assert handled is False


@pytest.mark.asyncio
async def test_goal_profile_first_message_starts_goal_from_message() -> None:
    from voidx.agent.domain.goal import GoalSpec

    service = _service("goal")
    service._runtime.summary = (
        '{"objective": "refactor the auth module to use typed contracts", '
        '"acceptance_condition": "all auth call sites use the new contracts and tests pass", '
        '"achievement_method": ""}'
    )

    handled = await service._handle_goal_first_message(
        "refactor the auth module to use typed contracts",
        thread_id="",
    )

    assert handled is True
    assert len(service._execution.goal_service.started) == 1
    parent, spec = service._execution.goal_service.started[0]
    assert parent == "host-session"
    assert isinstance(spec, GoalSpec)
    assert "refactor the auth module" in spec.objective


@pytest.mark.asyncio
async def test_goal_first_message_intake_failure_reports_and_consumes() -> None:
    service = _service("goal")
    service._runtime.summary = "I need more information."

    handled = await service._handle_goal_first_message("something vague", thread_id="")

    assert handled is True
    assert service._execution.goal_service.started == []


@pytest.mark.asyncio
async def test_autonomous_first_message_only_fires_once() -> None:
    service = _service("goal")
    service._runtime.summary = (
        '{"objective": "do it", "acceptance_condition": "done", "achievement_method": ""}'
    )

    first = await service._route_autonomous_first_message("do it", thread_id="")
    service._execution.session.message_count = 1
    second = await service._route_autonomous_first_message("more", thread_id="")

    assert first is True
    assert second is False
    assert len(service._execution.goal_service.started) == 1


@pytest.mark.asyncio
async def test_autonomous_first_message_coding_profile_falls_through() -> None:
    service = _service("coding")

    handled = await service._route_autonomous_first_message("hello", thread_id="")

    assert handled is False


@pytest.mark.asyncio
async def test_first_message_is_persisted_so_intake_fires_once(monkeypatch) -> None:
    saved: list[tuple[str, str]] = []
    from voidx.memory.session import MessageRow

    async def fake_save_message(row: MessageRow) -> int:
        saved.append((row.session_id, row.content))
        return 1

    monkeypatch.setattr("voidx.memory.service.save_message", fake_save_message)

    service = _service("goal")
    service._runtime.summary = (
        '{"objective": "do it", "acceptance_condition": "done", "achievement_method": ""}'
    )

    handled = await service._handle_goal_first_message("do it", thread_id="")

    assert handled is True
    assert service._execution.session.message_count == 1
    assert saved == [("host-session", "do it")]
