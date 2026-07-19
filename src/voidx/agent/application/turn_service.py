"""Single-turn lifecycle use case."""

import asyncio
from typing import Any

from voidx.agent.domain.events import AgentEvent, AgentEventKind
from voidx.agent.domain.state import AgentRuntime
from voidx.agent.ports.events import EventPublisher
from voidx.agent.ports.session import SessionStore
from voidx.agent.ports.turn_engine import TurnEngine


class TurnService:
    def __init__(
        self,
        engine: TurnEngine,
        sessions: SessionStore,
        events: EventPublisher,
    ) -> None:
        self._engine = engine
        self._sessions = sessions
        self._events = events

    async def run(
        self,
        session_id: str,
        user_text: str,
        runtime: AgentRuntime,
        *,
        display_text: str | None = None,
        context: Any | None = None,
    ) -> AgentRuntime:
        self._events.publish(AgentEvent(kind=AgentEventKind.TURN_STARTED))
        try:
            result = await self._engine.run(
                user_text,
                runtime,
                display_text=display_text,
                context=context,
            )
        except asyncio.CancelledError:
            if session_id:
                await self._sessions.save_runtime(session_id, runtime)
            self._events.publish(
                AgentEvent(kind=AgentEventKind.TURN_FAILED, metadata={"cancelled": True})
            )
            raise
        except Exception as exc:
            if session_id:
                await self._sessions.save_runtime(session_id, runtime)
            self._events.publish(
                AgentEvent(kind=AgentEventKind.TURN_FAILED, message=str(exc))
            )
            raise
        if session_id:
            await self._sessions.save_runtime(session_id, result)
        self._events.publish(AgentEvent(kind=AgentEventKind.TURN_COMPLETED))
        return result
