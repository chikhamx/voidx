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
