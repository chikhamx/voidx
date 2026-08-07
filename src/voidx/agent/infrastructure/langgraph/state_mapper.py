"""Mapping between application runtime and LangGraph execution state."""

from __future__ import annotations

from typing import Protocol

from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.task.intent import InteractionMode
from voidx.agent.domain.task.state import TaskState
from voidx.agent.domain.turn.state import TurnPhase


class LangGraphStateTarget(Protocol):
    interaction_mode: InteractionMode
    task_state: TaskState
    compaction_summary: str
    session_date: str

    def set_interaction_mode(self, mode: str | InteractionMode) -> InteractionMode: ...
    def set_task_state(self, task_state: TaskState) -> None: ...
    def set_compaction_summary(self, value: str) -> None: ...
    def set_session_date(self, value: str) -> None: ...


class LangGraphStateMapper:
    def apply_runtime(self, target: LangGraphStateTarget, runtime: SessionRuntimeState) -> None:
        target.set_interaction_mode(runtime.interaction_mode)
        target.set_task_state(runtime.task_state.model_copy(deep=True))
        target.set_compaction_summary(runtime.compaction_summary)
        target.set_session_date(runtime.session_time)

    def runtime_from_execution(
        self,
        source: LangGraphStateTarget,
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
