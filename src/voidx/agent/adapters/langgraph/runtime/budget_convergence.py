"""Pure budget convergence signals shared by agent runtimes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


BudgetDimension = Literal["step", "wall_clock", "context"]
ConvergenceLevel = Literal["none", "soft", "hard"]


@dataclass(frozen=True)
class BudgetReading:
    dimension: BudgetDimension
    current: float
    soft_limit: float
    hard_limit: float


@dataclass(frozen=True)
class ConvergenceDecision:
    triggered_dimensions: frozenset[BudgetDimension] = frozenset()
    level: ConvergenceLevel = "none"
    metadata: dict[str, float | str] = field(default_factory=dict)


@dataclass
class BudgetConvergenceState:
    soft_prompted: bool = False
    hard_prompted: bool = False


def decide_convergence(
    readings: list[BudgetReading],
    state: BudgetConvergenceState,
) -> ConvergenceDecision:
    if state.hard_prompted:
        return ConvergenceDecision()

    hard = [reading for reading in readings if reading.current >= reading.hard_limit]
    if hard:
        state.hard_prompted = True
        return _decision("hard", hard)

    soft = [reading for reading in readings if reading.current >= reading.soft_limit]
    if soft and not state.soft_prompted:
        state.soft_prompted = True
        return _decision("soft", soft)

    return ConvergenceDecision()


def _decision(
    level: ConvergenceLevel,
    readings: list[BudgetReading],
) -> ConvergenceDecision:
    metadata: dict[str, float | str] = {"level": level}
    for reading in readings:
        prefix = reading.dimension
        metadata[f"{prefix}.current"] = reading.current
        metadata[f"{prefix}.soft_limit"] = reading.soft_limit
        metadata[f"{prefix}.hard_limit"] = reading.hard_limit
    return ConvergenceDecision(
        triggered_dimensions=frozenset(reading.dimension for reading in readings),
        level=level,
        metadata=metadata,
    )
