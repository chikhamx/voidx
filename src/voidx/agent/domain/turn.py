"""Pure turn lifecycle state transitions."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voidx.agent.domain.state import AgentRuntime


class TurnPhase(str, Enum):
    INITIAL = "initial"
    RUNNING = "running"
    COMMITTED = "committed"


_ALLOWED_TRANSITIONS = {
    TurnPhase.INITIAL: frozenset({TurnPhase.RUNNING}),
    TurnPhase.RUNNING: frozenset({TurnPhase.COMMITTED}),
    TurnPhase.COMMITTED: frozenset(),
}


def advance_turn(runtime: AgentRuntime, phase: TurnPhase) -> AgentRuntime:
    """Return a copied runtime advanced to the next valid turn phase."""
    current = runtime.turn_phase
    if phase not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid turn transition: {current.value} -> {phase.value}")
    return runtime.model_copy(update={"turn_phase": phase}, deep=True)
