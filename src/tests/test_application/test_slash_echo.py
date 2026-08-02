from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

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

    async def dispatch(self, inp: str) -> bool:
        self.handled.append(inp)
        return True


class FakeApp:
    def consume_quiet_command(self, command: str) -> bool:
        return False

    def hide_command_output(self) -> None:
        return None


def _service(dock: FakeDock) -> AgentService:
    ui = SimpleNamespace(dock=dock, ui=SimpleNamespace(print=lambda *a, **k: None))
    execution = SimpleNamespace(
        ui=ui,
        slash=FakeSlash(),
        session=None,
        session_id="",
        workspace="",
    )
    return AgentService(execution, runtime=SimpleNamespace())


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/loop @doc", "/loop 5m check build", "/init"])
async def test_self_displaying_commands_skip_generic_echo(command: str) -> None:
    dock = FakeDock()
    service = _service(dock)

    keep_running, _ = await service._handle_user_input(FakeApp(), command)

    assert keep_running is True
    assert dock.echoed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/help", "/unknown"])
async def test_other_commands_keep_generic_echo(command: str) -> None:
    dock = FakeDock()
    service = _service(dock)

    await service._handle_user_input(FakeApp(), command)

    assert dock.echoed == [command]
