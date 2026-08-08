"""LangGraph adapters for narrow AgentService input ports."""

from __future__ import annotations

from typing import Any

from voidx.agent.ports.input import InputFrontend, InputRuntimeStatus


class LangGraphInputAdapter:
    def __init__(self, host: Any) -> None:
        self._host = host
        self._frontend: InputFrontend | None = None

    def bind_frontend(self, frontend: InputFrontend | None) -> None:
        self._frontend = frontend

    def consume_quiet_command(self, command: str) -> bool:
        if self._frontend is None:
            return False
        return self._frontend.consume_quiet_command(command)

    def hide_command_output(self) -> None:
        if self._frontend is None:
            return
        self._frontend.hide_command_output()

    def input_status(self) -> InputRuntimeStatus:
        session = self._host.session
        return InputRuntimeStatus(
            session_id=self._host.session_id,
            workspace=self._host.workspace,
            runtime_profile=getattr(session, "runtime_profile", "coding") if session else "coding",
        )

    async def dispatch_slash(self, command: str) -> bool:
        return bool(await self._host.slash.dispatch(command))
