"""First-message mode dispatch: goal/loop profiles start on the first message."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.infrastructure.input_router import LangGraphAutonomousInputRouter
from voidx.agent.ports.presentation import NullAgentEventPublisher


class FakeLoopService:
    def __init__(self):
        self.started: list[tuple[str | None, object]] = []

    async def start(self, parent_thread_id, spec):
        self.started.append((parent_thread_id, spec))
        return SimpleNamespace(
            active=True,
            loop_thread_id="loop:1",
            prompt_summary=spec.prompt[:80],
        )


class FakeGoalService:
    def __init__(self):
        self.started: list[tuple[str | None, object]] = []

    async def start(self, parent_thread_id, spec):
        self.started.append((parent_thread_id, spec))
        return SimpleNamespace(active=True, objective_summary=spec.objective, attempt_count=0, max_attempts=1)


class FakeIntakeRuntime:
    def __init__(self, summary: str = "", spec: dict | None = None):
        self.summary = summary
        self.spec = spec
        self.loop_spec = spec
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        controller = getattr(request.context, "goal_intake_controller", None)
        if self.loop_spec is not None:
            from voidx.agent.domain.automation.loop import LoopSpec

            loop_controller = getattr(request.context, "loop_intake_controller", None)
            if loop_controller is not None:
                await loop_controller.submit_init(LoopSpec(**self.loop_spec))
        if self.spec is not None and controller is not None:
            from voidx.agent.domain.automation.goal import GoalSpec

            await controller.submit_init(GoalSpec(**self.spec))
        return SimpleNamespace(final_assistant_summary=self.summary)


class FakeEvents:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.turns: list[str] = []

    def publish_message(self, message: str) -> None:
        self.messages.append(message)

    def start_turn(self, text: str) -> None:
        self.turns.append(text)


def _service(profile: str, **overrides):
    execution = SimpleNamespace(
        session_id="host-session",
        session=SimpleNamespace(id="host-session", runtime_profile=profile, message_count=0),
        loop_service=FakeLoopService(),
        goal_service=FakeGoalService(),
    )
    events = overrides.get("events", FakeEvents())
    runtime = overrides.get("runtime", FakeIntakeRuntime())
    guidance = SimpleNamespace(submit_guidance=lambda *_args, **_kwargs: False)
    return LangGraphAutonomousInputRouter(
        execution, runtime, events, guidance,
        chat_service=None, coding_service=None,
        loop_service=execution.loop_service, goal_service=execution.goal_service,
    )


@pytest.mark.asyncio
async def test_loop_profile_first_message_starts_dynamic_loop() -> None:
    from voidx.agent.domain.automation.loop import LoopSpec

    service = _service("loop")
    service._runtime.loop_spec = {"prompt": "fix the flaky test"}

    handled = await service.route_first_message("fix the flaky test", thread_id="")

    assert handled is True
    assert len(service._loop_service.started) == 1
    parent, spec = service._loop_service.started[0]
    assert parent == "host-session"
    assert isinstance(spec, LoopSpec)
    assert spec.prompt == "fix the flaky test"
    assert spec.interval_seconds is None


@pytest.mark.asyncio
async def test_loop_first_message_without_service_falls_through() -> None:
    service = _service("loop")
    service._loop_service = None

    handled = await service.route_first_message("hello", thread_id="")

    assert handled is False


@pytest.mark.asyncio
async def test_goal_profile_first_message_starts_goal_from_message() -> None:
    from voidx.agent.domain.automation.goal import GoalSpec

    service = _service("goal")
    service._runtime.spec = {
        "objective": "refactor the auth module to use typed contracts",
        "acceptance_condition": "all auth call sites use the new contracts and tests pass",
        "achievement_method": "",
    }

    handled = await service.route_first_message(
        "refactor the auth module to use typed contracts",
        thread_id="",
    )

    assert handled is True
    assert len(service._goal_service.started) == 1
    parent, spec = service._goal_service.started[0]
    assert parent == "host-session"
    assert isinstance(spec, GoalSpec)
    assert "refactor the auth module" in spec.objective


@pytest.mark.asyncio
async def test_goal_first_message_intake_failure_reports_and_consumes() -> None:
    service = _service("goal")
    service._runtime.summary = "I need more information."

    handled = await service.route_first_message("something vague", thread_id="")

    assert handled is True
    assert service._goal_service.started == []


@pytest.mark.asyncio
async def test_autonomous_first_message_only_fires_once() -> None:
    service = _service("goal")
    service._runtime.spec = {
        "objective": "do it",
        "acceptance_condition": "done",
        "achievement_method": "",
    }

    first = await service.route_first_message("do it", thread_id="")
    service._execution.session.message_count = 1
    second = await service.route_first_message("more", thread_id="")

    assert first is True
    assert second is False
    assert len(service._goal_service.started) == 1


@pytest.mark.asyncio
async def test_autonomous_first_message_coding_profile_falls_through() -> None:
    service = _service("coding")

    handled = await service.route_first_message("hello", thread_id="")

    assert handled is False


@pytest.mark.asyncio
async def test_first_message_is_persisted_so_intake_fires_once(monkeypatch) -> None:
    saved: list[tuple[str, str]] = []
    from voidx.agent.adapters.persistence.session_repository import MessageRow

    async def fake_save_message(row: MessageRow) -> int:
        saved.append((row.session_id, row.content))
        return 1

    monkeypatch.setattr("voidx.agent.adapters.persistence.session_repository.save_message", fake_save_message)

    service = _service("goal")
    service._runtime.spec = {
        "objective": "do it",
        "acceptance_condition": "done",
        "achievement_method": "",
    }

    handled = await service.route_first_message("do it", thread_id="")

    assert handled is True
    assert service._execution.session.message_count == 1
    assert saved == [("host-session", "do it")]


@pytest.mark.asyncio
async def test_goal_intake_failure_persists_first_message(monkeypatch) -> None:
    saved: list[tuple[str, str]] = []
    from voidx.agent.adapters.persistence.session_repository import MessageRow

    async def fake_save_message(row: MessageRow) -> int:
        saved.append((row.session_id, row.content))
        return 1

    monkeypatch.setattr("voidx.agent.adapters.persistence.session_repository.save_message", fake_save_message)

    service = _service("goal")
    service._runtime.summary = "I need a clearer acceptance condition."

    handled = await service.route_first_message("something vague", thread_id="")

    assert handled is True
    assert service._execution.session.message_count == 1
    assert saved == [("host-session", "something vague")]
    assert service._goal_service.started == []



@pytest.mark.asyncio
async def test_goal_profile_followup_does_not_fall_through_to_coding(monkeypatch) -> None:
    """After goal intake starts, later host-session messages must not run coding."""
    service = _service("goal")
    service._execution.session.message_count = 1
    service._goal_service = FakeGoalService()

    async def fake_status(_parent):
        return SimpleNamespace(
            active=True,
            objective_summary="ship the feature",
            attempt_count=1,
            max_attempts=20,
            state="running",
        )

    service._goal_service.status = fake_status  # type: ignore[method-assign]
    printed = service._events.messages

    handled = await service.route_followup("please also cover edge cases", thread_id="")

    assert handled is True
    assert any("goal" in line.lower() for line in printed)
