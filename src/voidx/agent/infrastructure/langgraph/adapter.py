"""Turn engine backed by LangGraph execution."""

from __future__ import annotations

from typing import Any

from voidx.agent.domain.state import AgentRuntime
from voidx.agent.domain.turn import TurnPhase
from voidx.agent.infrastructure.langgraph.state_mapper import LangGraphStateMapper


class LangGraphTurnEngine:
    def __init__(
        self,
        execution: Any,
        *,
        runner: Any | None = None,
        mapper: LangGraphStateMapper | None = None,
    ) -> None:
        self._execution = execution
        self._runner = runner
        self._mapper = mapper or LangGraphStateMapper()

    async def run(
        self,
        user_text: str,
        runtime: AgentRuntime,
        *,
        display_text: str | None = None,
        context: Any | None = None,
    ) -> AgentRuntime:
        self._mapper.apply_runtime(self._execution, runtime)
        if self._runner is None:
            await self._execution.run_turn(
                user_text,
                display_text=display_text,
                context=context,
            )
        else:
            await self._runner.run_once(
                user_text,
                display_text=display_text,
                context=context,
            )
        return self._mapper.runtime_from_execution(
            self._execution,
            turn_phase=TurnPhase.COMMITTED,
        )
