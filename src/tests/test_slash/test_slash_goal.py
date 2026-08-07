from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.slash import SlashHandler


def _capture_output(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr("voidx.agent.slash.handler.ui.print", lambda text="": output.append(str(text)))
    monkeypatch.setattr("voidx.agent.slash.handler.ui.error", lambda text="": output.append(f"ERROR: {text}"))
    return output


class FakeGoalService:
    def __init__(self) -> None:
        self.started: tuple[str | None, object] | None = None
        self.stopped: str | None = None
        self._status = None

    async def start(self, parent_thread_id: str | None, spec):
        self.started = (parent_thread_id, spec)
        self._status = SimpleNamespace(
            active=True,
            goal_thread_id=spec.goal_thread_id(parent_thread_id),
            objective_summary=spec.objective_summary(),
            attempt_count=0,
            max_attempts=spec.max_attempts,
            state="ready",
            last_evaluator_summary="",
        )
        return self._status

    async def status(self, parent_thread_id: str | None):
        return self._status

    async def stop(self, parent_thread_id: str | None):
        self.stopped = parent_thread_id
        self._status = None
        return True


def _host(service=None):
    return SimpleNamespace(
        goal_service=service,
        session=SimpleNamespace(id="session-1"),
        set_interaction_mode=lambda _mode: (_ for _ in ()).throw(AssertionError("legacy mode used")),
    )


@pytest.mark.asyncio
async def test_goal_command_starts_goal_runtime_with_acceptance(monkeypatch) -> None:
    output = _capture_output(monkeypatch)
    service = FakeGoalService()

    assert await SlashHandler(_host(service)).dispatch("/goal ship feature --accept tests pass") is True

    assert service.started is not None
    parent_id, spec = service.started
    assert parent_id == "session-1"
    assert spec.objective == "ship feature"
    assert spec.acceptance_condition == "tests pass"
    assert any("/goal started" in line for line in output)


@pytest.mark.asyncio
async def test_goal_status_and_stop(monkeypatch) -> None:
    output = _capture_output(monkeypatch)
    service = FakeGoalService()
    handler = SlashHandler(_host(service))

    await handler.dispatch("/goal ship --accept done")
    await handler.dispatch("/goal status")
    await handler.dispatch("/goal stop")

    assert service.stopped == "session-1"
    assert any("active" in line for line in output)
    assert any("stopped" in line for line in output)


@pytest.mark.asyncio
async def test_goal_requires_acceptance_condition(monkeypatch) -> None:
    output = _capture_output(monkeypatch)
    service = FakeGoalService()

    await SlashHandler(_host(service)).dispatch("/goal ship feature")

    assert service.started is None
    assert any("--accept" in line for line in output)


@pytest.mark.asyncio
async def test_goal_without_args_switches_profile(monkeypatch) -> None:
    switches: list[str] = []

    class Handler(SlashHandler):
        async def _switch_profile(self, profile: str) -> None:
            switches.append(profile)

    assert await Handler(_host()).dispatch("/goal") is True

    assert switches == ["goal"]
