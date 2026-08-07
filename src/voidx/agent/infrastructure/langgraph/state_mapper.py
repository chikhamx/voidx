"""Mapping between application runtime and LangGraph execution state."""

from __future__ import annotations

from voidx.agent.ports.execution_host import ExecutionHost

from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.turn.state import TurnPhase


class LangGraphStateMapper:
    def apply_runtime(self, target: ExecutionHost, runtime: SessionRuntimeState) -> None:
        target.set_interaction_mode(runtime.interaction_mode)
        target.set_task_state(runtime.task_state.model_copy(deep=True))
        target.set_compaction_summary(runtime.compaction_summary)
        target.set_session_date(runtime.session_time)

    def runtime_from_execution(
        self,
        source: ExecutionHost,
        *,
        turn_phase: TurnPhase,
    ) -> SessionRuntimeState:
        return SessionRuntimeState(
            interaction_mode=source.interaction_mode,
            task_state=source.task_state.model_copy(deep=True),
            compaction_summary=source.compaction_summary,
            session_time=source.session_date,
            turn_phase=turn_phase,
        )
