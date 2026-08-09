from __future__ import annotations

from voidx.agent.adapters.langgraph.runtime.budget_convergence import (
    BudgetConvergenceState,
    BudgetReading,
    decide_convergence,
)


def _reading(
    dimension: str,
    *,
    current: float,
    soft_limit: float = 80.0,
    hard_limit: float = 100.0,
) -> BudgetReading:
    return BudgetReading(
        dimension=dimension,
        current=current,
        soft_limit=soft_limit,
        hard_limit=hard_limit,
    )


def test_convergence_emits_no_signal_below_all_limits() -> None:
    state = BudgetConvergenceState()

    decision = decide_convergence(
        [
            _reading("step", current=79),
            _reading("wall_clock", current=60),
            _reading("context", current=70, soft_limit=75, hard_limit=90),
        ],
        state,
    )

    assert decision.level == "none"
    assert decision.triggered_dimensions == frozenset()
    assert not hasattr(decision, "action")
    assert state == BudgetConvergenceState()


def test_convergence_emits_soft_once_and_merges_dimensions() -> None:
    state = BudgetConvergenceState()
    readings = [
        _reading("step", current=80),
        _reading("wall_clock", current=81),
        _reading("context", current=70, soft_limit=75, hard_limit=90),
    ]

    first = decide_convergence(readings, state)
    second = decide_convergence(readings, state)

    assert first.level == "soft"
    assert first.triggered_dimensions == frozenset({"step", "wall_clock"})
    assert first.metadata["step.current"] == 80
    assert first.metadata["wall_clock.current"] == 81
    assert state.soft_prompted is True
    assert second.level == "none"


def test_convergence_emits_hard_once_and_hard_wins() -> None:
    state = BudgetConvergenceState()
    readings = [
        _reading("step", current=80),
        _reading("wall_clock", current=100),
        _reading("context", current=95, soft_limit=75, hard_limit=90),
    ]

    first = decide_convergence(readings, state)
    second = decide_convergence(readings, state)

    assert first.level == "hard"
    assert first.triggered_dimensions == frozenset({"wall_clock", "context"})
    assert state.hard_prompted is True
    assert second.level == "none"


def test_hard_signal_after_soft_is_still_emitted_once() -> None:
    state = BudgetConvergenceState()

    soft = decide_convergence([_reading("step", current=80)], state)
    hard = decide_convergence([_reading("step", current=100)], state)
    repeated = decide_convergence([_reading("step", current=100)], state)

    assert soft.level == "soft"
    assert hard.level == "hard"
    assert repeated.level == "none"
