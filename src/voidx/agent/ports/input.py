"""Narrow input orchestration ports for AgentService."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class InputRuntimeStatus:
    session_id: str = ""
    workspace: str = ""
    runtime_profile: str = "coding"


class InputStatusReader(Protocol):
    def input_status(self) -> InputRuntimeStatus: ...


class InputFrontend(Protocol):
    def consume_quiet_command(self, command: str) -> bool: ...
    def hide_command_output(self) -> None: ...


class InputFrontendBinder(Protocol):
    def bind_frontend(self, frontend: InputFrontend | None) -> None: ...


class SlashCommandDispatcher(Protocol):
    def bind_frontend(self, frontend: InputFrontend | None) -> None: ...
    def consume_quiet_command(self, command: str) -> bool: ...
    def hide_command_output(self) -> None: ...
    async def dispatch_slash(self, command: str) -> bool: ...


class AutonomousInputRouter(Protocol):
    async def route_first_message(self, text: str, *, thread_id: str = "") -> bool: ...
    async def route_followup(self, text: str, *, thread_id: str = "") -> bool: ...
    async def route_chat_turn(self, text: str, *, thread_id: str = "") -> bool: ...
    async def run_coding_turn(
        self,
        text: str,
        *,
        thread_id: str = "",
        context: object | None = None,
        display_text: str | None = None,
    ) -> None: ...
    def start_turn(self, text: str) -> None: ...
    def publish_message(self, message: str) -> None: ...
