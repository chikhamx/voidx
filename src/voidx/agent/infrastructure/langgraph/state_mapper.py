"""Mapping between application runtime and LangGraph execution state."""

from __future__ import annotations

from typing import Any

from voidx.agent.domain.state import AgentRuntime
from voidx.agent.domain.turn import TurnPhase


class LangGraphStateMapper:
    def apply_runtime(self, target: Any, runtime: AgentRuntime) -> None:
        target._interaction_mode = runtime.interaction_mode
        target._task_state = runtime.task_state.model_copy(deep=True)
        target._compaction_summary = runtime.compaction_summary
        target._session_date = runtime.session_time

    def runtime_from_execution(
        self,
        source: Any,
        *,
        turn_phase: TurnPhase,
    ) -> AgentRuntime:
        return AgentRuntime(
            interaction_mode=source._interaction_mode,
            task_state=source._task_state.model_copy(deep=True),
            compaction_summary=source._compaction_summary,
            session_time=source._session_date,
            turn_phase=turn_phase,
        )
