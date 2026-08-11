"""Narrow AgentService input port fakes."""

from __future__ import annotations

from voidx.agent.ports.input import InputRuntimeStatus


class FakeInputPorts:
    def __init__(self, *, session_id: str = "", workspace: str = "", runtime_profile: str = "coding") -> None:
        self.status = InputRuntimeStatus(session_id, workspace, runtime_profile)
        self.slash_commands: list[str] = []
        self.frontend = None

    def bind_frontend(self, frontend) -> None:
        self.frontend = frontend

    def consume_quiet_command(self, command: str) -> bool:
        return self.frontend.consume_quiet_command(command) if self.frontend is not None else False

    def hide_command_output(self) -> None:
        if self.frontend is not None:
            self.frontend.hide_command_output()

    def input_status(self) -> InputRuntimeStatus:
        return self.status

    async def dispatch_slash(self, command: str) -> bool:
        self.slash_commands.append(command)
        return False

    async def route_first_message(self, text: str, *, thread_id: str = "") -> bool:
        return False

    async def route_followup(self, text: str, *, thread_id: str = "") -> bool:
        return False


    def start_turn(self, text: str) -> None:
        return None

    def publish_message(self, message: str) -> None:
        return None

    async def route_chat_turn(self, text: str, *, thread_id: str = "", context=None) -> bool:
        return False

    async def run_coding_turn(
        self,
        text: str,
        *,
        thread_id: str = "",
        context=None,
        display_text: str | None = None,
    ) -> None:
        return None
    def can_submit_guidance(self) -> bool:
        return False

    def submit_guidance(self, text: str, **kwargs) -> bool:
        return False


def service_ports(**kwargs):
    ports = FakeInputPorts(**kwargs)
    return ports, ports, ports, ports
