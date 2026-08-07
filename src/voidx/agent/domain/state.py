"""Agent-owned domain runtime state."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.agent.domain.turn.state import TurnPhase
from voidx.agent.domain.task.intent import InteractionMode
from voidx.agent.domain.task.state import TaskState


class SessionRuntimeState(BaseModel):
    """Mutable Agent state independent of graph and persistence adapters."""

    interaction_mode: InteractionMode = InteractionMode.AUTO
    task_state: TaskState = Field(default_factory=TaskState)
    compaction_summary: str = ""
    session_time: str = ""
    turn_phase: TurnPhase = TurnPhase.INITIAL
