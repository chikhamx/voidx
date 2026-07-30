"""Turn engine backed by LangGraph execution."""

from __future__ import annotations

from typing import Any

from voidx.agent.ports.execution_host import ExecutionHost

from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.turn import TurnPhase
from voidx.agent.infrastructure.langgraph.state_mapper import LangGraphStateMapper


class LangGraphTurnEngine:
    def __init__(
        self,
        execution: ExecutionHost,
        *,
        mapper: LangGraphStateMapper | None = None,
    ) -> None:
        self._execution = execution
        self._mapper = mapper or LangGraphStateMapper()

    @property
    def session_id(self) -> str:
        return getattr(self._execution, "session_id", "") or ""

    async def run(
        self,
        user_text: str,
        runtime: SessionRuntimeState,
        *,
        display_text: str | None = None,
        context: Any | None = None,
        persist_user_input: bool = True,
    ) -> SessionRuntimeState:
        self._mapper.apply_runtime(self._execution, runtime)
        await self._execution.run_turn(
            user_text,
            display_text=display_text,
            context=context,
            persist_user_input=persist_user_input,
        )
        # Return the post-execution state still in RUNNING phase; the runtime
        # facade owns the COMMITTED transition via advance_turn.
        return self._mapper.runtime_from_execution(
            self._execution,
            turn_phase=TurnPhase.RUNNING,
        )
