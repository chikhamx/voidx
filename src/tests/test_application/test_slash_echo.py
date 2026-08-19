from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from tests.test_application.input_ports import FakeInputPorts
from voidx.agent.application.agent_service import AgentService


@dataclass
class FakeDock:
    echoed: list[str] = field(default_factory=list)

    def start_turn(self, text: str):
        self.echoed.append(text)
        return object()


@dataclass
class FakeSlash:
    handled: list[str] = field(default_factory=list)
    error: BaseException | None = None

    async def dispatch(self, inp: str) -> bool:
        self.handled.append(inp)
        if self.error is not None:
            raise self.error
        return True


class FakeApp:
    def consume_quiet_command(self, command: str) -> bool:
        return False

    def hide_command_output(self) -> None:
        return None


class QuietApp(FakeApp):
    def consume_quiet_command(self, command: str) -> bool:
        return True


class FakeEvents:
    def __init__(self, dock: FakeDock) -> None:
        self._dock = dock
        self.completed = 0
        self.failed: list[str] = []
        self.cancelled = 0

    def publish_message(self, _message: str) -> None:
        return None

    def start_turn(self, text: str) -> None:
        self._dock.start_turn(text)

    def end_turn(self) -> None:
        self.completed += 1

    def fail_turn(self, message: str) -> None:
        self.failed.append(message)

    def cancel_turn(self) -> None:
        self.cancelled += 1


def _service(
    dock: FakeDock,
    *,
    slash_error: BaseException | None = None,
) -> tuple[AgentService, FakeEvents]:
    ports = FakeInputPorts()
    slash = FakeSlash(error=slash_error)

    async def dispatch(command: str) -> bool:
        return await slash.dispatch(command)

    ports.dispatch_slash = dispatch
    events = FakeEvents(dock)
    ports.start_turn = events.start_turn
    ports.end_turn = events.end_turn
    ports.fail_turn = events.fail_turn
    ports.cancel_turn = events.cancel_turn
    ports.publish_message = events.publish_message
    return AgentService(ports, ports, ports, ports), events


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/loop @doc", "/loop 5m check build", "/init"])
async def test_self_displaying_commands_skip_generic_echo(command: str) -> None:
    dock = FakeDock()
    service, events = _service(dock)

    service._slash_dispatcher.bind_frontend(FakeApp())
    keep_running, _ = await service.dispatch_input(command)

    assert keep_running is True
    assert dock.echoed == []
    assert events.completed == 0
    assert events.failed == []
    assert events.cancelled == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/help", "/unknown"])
async def test_other_commands_keep_generic_echo(command: str) -> None:
    dock = FakeDock()
    service, events = _service(dock)

    service._slash_dispatcher.bind_frontend(FakeApp())
    await service.dispatch_input(command)

    assert dock.echoed == [command]
    assert events.completed == 1
    assert events.failed == []
    assert events.cancelled == 0


@pytest.mark.asyncio
async def test_slash_command_completes_turn() -> None:
    dock = FakeDock()
    service, events = _service(dock)

    service._slash_dispatcher.bind_frontend(FakeApp())
    await service.dispatch_input("/model switch deepseek/deepseek-v4-flash --local")

    assert dock.echoed == ["/model switch deepseek/deepseek-v4-flash --local"]
    assert events.completed == 1
    assert events.failed == []
    assert events.cancelled == 0


@pytest.mark.asyncio
async def test_slash_command_failure_fails_turn() -> None:
    dock = FakeDock()
    service, events = _service(dock, slash_error=RuntimeError("boom"))

    service._slash_dispatcher.bind_frontend(FakeApp())
    with pytest.raises(RuntimeError, match="boom"):
        await service.dispatch_input("/help")

    assert dock.echoed == ["/help"]
    assert events.completed == 0
    assert events.failed == ["boom"]
    assert events.cancelled == 0


@pytest.mark.asyncio
async def test_cancelled_slash_command_cancels_turn() -> None:
    dock = FakeDock()
    service, events = _service(dock, slash_error=asyncio.CancelledError())

    service._slash_dispatcher.bind_frontend(FakeApp())
    with pytest.raises(asyncio.CancelledError):
        await service.dispatch_input("/model switch deepseek/deepseek-v4-flash --local")

    assert dock.echoed == ["/model switch deepseek/deepseek-v4-flash --local"]
    assert events.completed == 0
    assert events.failed == []
    assert events.cancelled == 1


@pytest.mark.asyncio
async def test_quiet_command_skips_turn_lifecycle() -> None:
    dock = FakeDock()
    service, events = _service(dock)

    service._slash_dispatcher.bind_frontend(QuietApp())
    await service.dispatch_input("/quiet-cmd")

    assert dock.echoed == []
    assert events.completed == 0
    assert events.failed == []
    assert events.cancelled == 0