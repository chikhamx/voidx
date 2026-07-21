"""Compatibility adapter for the reusable Agent Runtime."""

from __future__ import annotations

from typing import Any

from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.thread import AgentThread
from voidx.agent.ports.events import EventPublisher
from voidx.agent.ports.session import SessionStore
from voidx.agent.ports.turn_engine import TurnEngine
from voidx.agent.runtime import AgentRuntime
from voidx.agent.runtime.contracts import TurnRequest


class TurnService:
    """Legacy call shape delegating all turn ownership to AgentRuntime."""

    def __init__(
        self,
        engine: TurnEngine | None = None,
        sessions: SessionStore | None = None,
        events: EventPublisher | None = None,
        *,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self._runtime = runtime or AgentRuntime(
            type(
                "RuntimeResourcesAdapter",
                (),
                {"turn_engine": engine, "sessions": sessions, "events": events},
            )()
        )

    async def run_turn(self, request: TurnRequest):
        return await self._runtime.run_turn(request)

    async def run(
        self,
        session_id: str,
        user_text: str,
        runtime: SessionRuntimeState,
        *,
        display_text: str | None = None,
        context: Any | None = None,
    ) -> SessionRuntimeState:
        result = await self.run_turn(
            TurnRequest(
                thread=AgentThread(
                    thread_id=session_id or "coding",
                    session_id=session_id or None,
                ),
                user_text=user_text,
                runtime=runtime,
                display_text=display_text,
                context=context,
            )
        )
        return result.runtime
