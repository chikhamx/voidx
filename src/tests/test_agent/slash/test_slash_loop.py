from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.loop.prompt_source import PromptSource
from voidx.agent.slash import SlashHandler
from tests.test_agent.slash.context import command_context


class FakeLoopManager:
    def __init__(self) -> None:
        self.started: tuple[PromptSource, float | None] | None = None
        self.start_kwargs: dict = {}
        self.stopped = False
        self._status: dict | None = None

    def start(self, prompt_source: PromptSource, interval_seconds: float | None, **kwargs) -> None:
        self.started = (prompt_source, interval_seconds)
        self.start_kwargs = kwargs
        self._status = {"active": True, "mode": "dynamic" if interval_seconds is None else "fixed"}

    def stop(self) -> None:
        self.stopped = True
        self._status = None

    def status(self) -> dict | None:
        return self._status


def _graph(tmp_path, manager: FakeLoopManager):
    return SimpleNamespace(workspace=str(tmp_path), loop_manager=manager, session=None)


def _capture_output(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr("voidx.agent.slash.handler.ui.print", lambda text="": output.append(str(text)))
    monkeypatch.setattr("voidx.agent.slash.handler.ui.error", lambda text="": output.append(f"ERROR: {text}"))
    return output


@pytest.mark.asyncio
async def test_loop_command_starts_fixed_loop(tmp_path, monkeypatch) -> None:
    _capture_output(monkeypatch)
    manager = FakeLoopManager()

    assert await SlashHandler(_graph(tmp_path, manager)).dispatch("/loop 5m check build") is True

    assert manager.started is not None
    source, interval = manager.started
    assert source.raw == "check build"
    assert interval == 300


@pytest.mark.asyncio
async def test_loop_command_starts_dynamic_loop(tmp_path, monkeypatch) -> None:
    _capture_output(monkeypatch)
    manager = FakeLoopManager()

    assert await SlashHandler(_graph(tmp_path, manager)).dispatch("/loop check deploy") is True

    assert manager.started is not None
    source, interval = manager.started
    assert source.raw == "check deploy"
    assert interval is None
    assert manager.start_kwargs["bash_tool"] is not None
    assert manager.start_kwargs["ctx"].loop_manager is manager


@pytest.mark.asyncio
async def test_loop_stop_and_status(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)
    manager = FakeLoopManager()
    handler = SlashHandler(_graph(tmp_path, manager))

    await handler.dispatch("/loop 5m check build")
    await handler.dispatch("/loop status")
    await handler.dispatch("/loop stop")

    assert manager.stopped is True
    assert any("active" in line for line in output)
    assert any("stopped" in line for line in output)


class FakeLoopService:
    def __init__(self) -> None:
        self.started: tuple[str | None, object] | None = None
        self.stopped: str | None = None
        self._status = SimpleNamespace(active=True, mode="fixed", loop_thread_id="loop:session-1:active")

    async def start(self, parent_thread_id: str | None, spec):
        self.started = (parent_thread_id, spec)
        self._status = SimpleNamespace(
            active=True,
            mode=spec.mode.value,
            interval_seconds=spec.interval_seconds,
            loop_thread_id=spec.loop_thread_id(parent_thread_id),
        )
        return self._status

    async def status(self, parent_thread_id: str | None):
        return self._status

    async def stop(self, parent_thread_id: str | None):
        self.stopped = parent_thread_id
        self._status = None
        return True


@pytest.mark.asyncio
async def test_loop_command_prefers_loop_service_when_available(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)
    manager = FakeLoopManager()
    service = FakeLoopService()
    host = SimpleNamespace(
        workspace=str(tmp_path),
        loop_manager=manager,
        loop_service=service,
        session=SimpleNamespace(id="session-1"),
    )
    handler = SlashHandler(host)

    assert await handler.dispatch("/loop 60s check runtime") is True
    await handler.dispatch("/loop status")
    await handler.dispatch("/loop stop")

    assert manager.started is None
    assert service.started is not None
    parent_id, spec = service.started
    assert parent_id == "session-1"
    assert spec.prompt == "check runtime"
    assert spec.interval_seconds == 60
    assert service.stopped == "session-1"
    assert any("loop:session-1:active" in line for line in output)
