"""Reusable single-turn Agent Runtime facade."""

from __future__ import annotations

import asyncio
from typing import Any

from voidx.agent.domain.events import AgentEvent, AgentEventKind
from voidx.agent.domain.thread import AgentThread, LifecycleState
from voidx.agent.domain.turn import TurnPhase
from voidx.agent.runtime.contracts import TurnRequest, TurnResult
from voidx.agent.ports.runtime_resources import RuntimeResources


class AgentRuntime:
    """Resolve, execute and commit one turn through shared agent infrastructure."""

    def __init__(self, resources: RuntimeResources) -> None:
        self._resources = resources

    async def run(
        self,
        session_id: str,
        user_text: str,
        runtime,
        *,
        display_text: str | None = None,
        context: Any | None = None,
    ):
        """Compatibility-shaped coding call routed through the runtime facade."""
        thread = AgentThread(thread_id=session_id or "coding", session_id=session_id or None)
        result = await self.run_turn(
            TurnRequest(
                thread=thread,
                user_text=user_text,
                runtime=runtime,
                display_text=display_text,
                context=context,
            )
        )
        return result.runtime

    async def run_turn(
        self,
        request: TurnRequest,
        *,
        session_id: str | None = None,
        user_text: str | None = None,
        display_text: str | None = None,
        context: Any | None = None,
    ) -> TurnResult:
        thread = request.thread
        resolved_session_id = session_id or thread.session_id
        runtime = request.runtime
        text = request.user_text if user_text is None else user_text
        shown = request.display_text if display_text is None else display_text
        ctx = request.context if context is None else context
        if resolved_session_id:
            runtime = await self._resources.sessions.load_runtime(resolved_session_id)

        self._resources.events.publish(AgentEvent(kind=AgentEventKind.TURN_STARTED))
        runtime = runtime.model_copy(update={"turn_phase": TurnPhase.RUNNING}, deep=True)
        running_thread = thread.model_copy(update={"session_id": resolved_session_id, "lifecycle": LifecycleState.RUNNING})
        try:
            result = await self._resources.turn_engine.run(
                text,
                runtime,
                display_text=shown,
                context=ctx,
            )
        except asyncio.CancelledError:
            if resolved_session_id:
                await self._resources.sessions.save_runtime(resolved_session_id, runtime)
            self._resources.events.publish(AgentEvent(kind=AgentEventKind.TURN_FAILED, metadata={"cancelled": True}))
            raise
        except Exception as exc:
            if resolved_session_id:
                await self._resources.sessions.save_runtime(resolved_session_id, runtime)
            self._resources.events.publish(AgentEvent(kind=AgentEventKind.TURN_FAILED, message=str(exc)))
            raise

        result = result.model_copy(update={"turn_phase": TurnPhase.COMMITTED}, deep=True)
        final_session_id = resolved_session_id or getattr(self._resources.turn_engine, "session_id", None)
        if final_session_id:
            await self._resources.sessions.save_runtime(final_session_id, result)
        committed_thread = running_thread.model_copy(update={"session_id": final_session_id, "lifecycle": LifecycleState.COMPLETED})
        self._resources.events.publish(AgentEvent(kind=AgentEventKind.TURN_COMPLETED))
        return TurnResult(thread=committed_thread, lifecycle=LifecycleState.COMPLETED, runtime=result)
