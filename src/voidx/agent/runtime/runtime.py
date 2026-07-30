"""Reusable single-turn Agent Runtime facade."""

from __future__ import annotations

import asyncio

from voidx.agent.domain.events import AgentEvent, AgentEventKind
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.thread import LifecycleState
from voidx.agent.domain.turn import TurnPhase, advance_turn
from voidx.agent.runtime.contracts import TurnRequest, TurnResult
from voidx.agent.ports.runtime_resources import RuntimeResources


class AgentRuntime:
    """Resolve, execute and commit one turn through shared agent infrastructure.

    ``run_turn(TurnRequest)`` is the single production entry for every profile
    (coding, chat, and future loop/goal). Identity and input come only from the
    frozen request; the runtime owns turn-event publication and runtime-state
    commit exactly once per turn.
    """

    def __init__(self, resources: RuntimeResources) -> None:
        self._resources = resources

    async def run_turn(self, request: TurnRequest) -> TurnResult:
        thread = request.thread
        resolved_session_id = thread.session_id
        runtime = request.runtime
        if runtime is None:
            runtime = (
                await self._resources.sessions.load_runtime(resolved_session_id)
                if resolved_session_id
                else SessionRuntimeState()
            )

        self._resources.events.publish(AgentEvent(kind=AgentEventKind.TURN_STARTED))
        runtime = advance_turn(runtime, TurnPhase.RUNNING)
        running_thread = thread.model_copy(
            update={"lifecycle": LifecycleState.RUNNING}
        )
        try:
            result = await self._resources.turn_engine.run(
                request.user_text,
                runtime,
                display_text=request.display_text,
                context=request.context,
                persist_user_input=request.persist_user_input,
            )
        except asyncio.CancelledError:
            if resolved_session_id:
                await self._resources.sessions.save_runtime(resolved_session_id, runtime)
            self._resources.events.publish(
                AgentEvent(kind=AgentEventKind.TURN_FAILED, metadata={"cancelled": True})
            )
            raise
        except Exception as exc:
            if resolved_session_id:
                await self._resources.sessions.save_runtime(resolved_session_id, runtime)
            self._resources.events.publish(
                AgentEvent(kind=AgentEventKind.TURN_FAILED, message=str(exc))
            )
            raise

        result = advance_turn(result, TurnPhase.COMMITTED)
        final_session_id = resolved_session_id or self._resources.turn_engine.session_id or None
        if final_session_id:
            await self._resources.sessions.save_runtime(final_session_id, result)
        committed_thread = running_thread.model_copy(
            update={"session_id": final_session_id, "lifecycle": LifecycleState.COMPLETED}
        )
        self._resources.events.publish(AgentEvent(kind=AgentEventKind.TURN_COMPLETED))
        return TurnResult(
            thread=committed_thread, lifecycle=LifecycleState.COMPLETED, runtime=result
        )
