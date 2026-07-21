"""Pure turn lifecycle state transitions and execution identity."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from voidx.agent.domain.state import SessionRuntimeState


class TurnPhase(str, Enum):
    INITIAL = "initial"
    RUNNING = "running"
    COMMITTED = "committed"


class TurnExecution(BaseModel):
    """Resolved immutable identity for one runtime turn."""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    session_id: str | None = None
    phase: TurnPhase = TurnPhase.INITIAL


_ALLOWED_TRANSITIONS = {
    TurnPhase.INITIAL: frozenset({TurnPhase.RUNNING}),
    TurnPhase.RUNNING: frozenset({TurnPhase.COMMITTED}),
    TurnPhase.COMMITTED: frozenset(),
}


def advance_turn(runtime: SessionRuntimeState, phase: TurnPhase) -> SessionRuntimeState:
    """Return a copied runtime advanced to the next valid turn phase."""
    current = runtime.turn_phase
    if phase not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid turn transition: {current.value} -> {phase.value}")
    return runtime.model_copy(update={"turn_phase": phase}, deep=True)
