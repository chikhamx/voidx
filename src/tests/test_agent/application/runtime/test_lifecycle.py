from __future__ import annotations

import pytest

from voidx.agent.domain.thread import LifecycleState, RuntimeDecision, apply_lifecycle_decision


def test_lifecycle_continue_moves_running_thread_to_waiting() -> None:
    state = apply_lifecycle_decision(
        LifecycleState.RUNNING,
        RuntimeDecision(outcome="continue", summary="keep going", progress="meaningful"),
    )

    assert state is LifecycleState.WAITING


def test_lifecycle_completed_is_terminal_and_not_overwritten() -> None:
    with pytest.raises(ValueError, match="terminal"):
        apply_lifecycle_decision(
            LifecycleState.COMPLETED,
            RuntimeDecision(outcome="continue", summary="late wakeup"),
        )


def test_lifecycle_controller_clamps_continue_delay() -> None:
    from voidx.agent.application.runtime.lifecycle import ContinuationPolicy, LifecycleController

    controller = LifecycleController(
        ContinuationPolicy(min_delay_seconds=60, max_delay_seconds=300)
    )

    decision = controller.normalize_decision(
        RuntimeDecision(outcome="continue", summary="later", next_delay_seconds=10)
    )

    assert decision.next_delay_seconds == 60


def test_lifecycle_cancel_takes_precedence_over_continue() -> None:
    state = apply_lifecycle_decision(
        LifecycleState.CANCELLING,
        RuntimeDecision(outcome="continue", summary="model wanted retry"),
    )

    assert state is LifecycleState.CANCELLED
