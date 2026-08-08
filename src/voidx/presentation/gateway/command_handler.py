"""Web gateway command adapter for agent application input."""

from __future__ import annotations

from typing import Protocol

from voidx.agent.domain.profile import CODING_PROFILE
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.ports.presentation import GatewayThreadRegistry, GuidancePort, RuntimeStatusReader
from voidx.presentation.protocol import UiCancelCommand, UiSubmitCommand, parse_ui_command


class GatewayFrontend(Protocol):
    def submit_external_input(self, text: str, *, context: TurnExecutionContext) -> None: ...
    def cancel_external_input(self, *, context: TurnExecutionContext) -> None: ...


class GatewayCommandHandler:
    def __init__(
        self,
        status_reader: RuntimeStatusReader,
        guidance: GuidancePort,
        thread_registry: GatewayThreadRegistry,
    ) -> None:
        self._status_reader = status_reader
        self._guidance = guidance
        self._thread_registry = thread_registry

    async def handle(self, app: GatewayFrontend, command: object) -> None:
        if isinstance(command, dict) and command.get("kind") == "guide":
            self._guidance.submit_guidance(str(command.get("text", "")), source="user")
            return
        parsed = parse_ui_command(command)
        status = self._status_reader.runtime_status()
        if isinstance(parsed, UiSubmitCommand):
            text = parsed.text
            if text.strip().startswith("/guide "):
                self._guidance.submit_guidance(text.strip().removeprefix("/guide").strip(), source="user")
                return
            self._thread_registry.ensure_thread(status.session)
            context = _turn_context(parsed.thread_id, status.session.session_id, status.workspace)
            app.submit_external_input(text, context=context)
        elif isinstance(parsed, UiCancelCommand):
            context = _turn_context(parsed.thread_id, status.session.session_id, status.workspace)
            app.cancel_external_input(context=context)


def _turn_context(thread_id: str, active_session_id: str, workspace: str) -> TurnExecutionContext:
    session_id = active_session_id or thread_id
    return TurnExecutionContext(
        thread_id=thread_id or session_id or "coding",
        session_id=session_id,
        runtime_profile=CODING_PROFILE,
        workspace=workspace,
    )
