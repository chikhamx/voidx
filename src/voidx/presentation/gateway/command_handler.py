"""Web gateway command adapter for agent application input."""

from __future__ import annotations

from typing import Any

from voidx.agent.application.coding_service import CODING_PROFILE
from voidx.agent.domain.turn_context import TurnExecutionContext


def ui_command_kind(command: Any) -> str:
    return str(getattr(command, "kind", "") or "")


class GatewayCommandHandler:
    """Translate gateway UI commands into application service calls."""

    def __init__(self, execution: Any, service: Any) -> None:
        self._execution = execution
        self._service = service

    async def handle(self, app: Any, command: Any) -> None:
        if isinstance(command, dict) and command.get("kind") == "guide":
            self._service.submit_guidance(str(command.get("text", "")), source="user")
            return
        kind = ui_command_kind(command)
        if kind == "submit":
            text = command.text
            if text.strip().startswith("/guide "):
                self._service.submit_guidance(text.strip().removeprefix("/guide").strip(), source="user")
            else:
                self.ensure_gateway_thread()
                thread_id = str(getattr(command, "thread_id", "") or "")
                session_id = self._execution.session_id or thread_id
                resolved_thread_id = thread_id or session_id or "coding"
                context = TurnExecutionContext(
                    thread_id=resolved_thread_id,
                    session_id=session_id,
                    runtime_profile=CODING_PROFILE,
                    workspace=self._execution.workspace,
                )
                app.submit_external_input(text, context=context)
        elif kind == "cancel":
            thread_id = str(getattr(command, "thread_id", "") or "")
            session_id = self._execution.session_id or thread_id
            resolved_thread_id = thread_id or session_id or "coding"
            context = TurnExecutionContext(
                thread_id=resolved_thread_id,
                session_id=session_id,
                runtime_profile=CODING_PROFILE,
                workspace=self._execution.workspace,
            )
            app.cancel_external_input(context=context)

    def ensure_gateway_thread(self) -> None:
        """Register the active session as a gateway thread if not yet registered."""
        import asyncio

        gs = self._execution.gateway_session
        if gs is None or self._execution.session is None:
            return
        tid = self._execution.session.id
        if tid and tid not in gs._threads:
            asyncio.ensure_future(
                gs.register_thread(
                    tid,
                    title=self._execution.session.title or "",
                    directory=getattr(self._execution.session, "directory", "") or "",
                )
            )
