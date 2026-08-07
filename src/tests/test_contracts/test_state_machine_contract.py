from __future__ import annotations

from voidx.agent.domain.automation.workflow import (
    WorkflowRunStatus,
    source_from_reason,
)
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.thread import (
    LifecycleState,
    RuntimeDecision,
    apply_lifecycle_decision,
)
from voidx.agent.domain.turn.state import TurnPhase, advance_turn

from .snapshot import assert_snapshot


def _capture(operation) -> dict[str, str]:
    try:
        value = operation()
    except Exception as exc:
        return {"error_type": type(exc).__name__, "error": str(exc)}
    return {"result": value.value}


def test_state_machine_contract() -> None:
    turn = []
    for current in TurnPhase:
        for target in TurnPhase:
            runtime = SessionRuntimeState(turn_phase=current)
            turn.append(
                {
                    "current": current.value,
                    "target": target.value,
                    "outcome": _capture(lambda runtime=runtime, target=target: advance_turn(runtime, target).turn_phase),
                }
            )

    lifecycle = []
    outcomes = ("continue", "completed", "blocked", "needs_user", "failed", "stop")
    for current in LifecycleState:
        for outcome in outcomes:
            decision = RuntimeDecision(outcome=outcome, summary="fixed summary")
            lifecycle.append(
                {
                    "current": current.value,
                    "decision": outcome,
                    "outcome": _capture(
                        lambda current=current, decision=decision: apply_lifecycle_decision(current, decision)
                    ),
                }
            )

    reasons = ["explicit", "trigger:file", "name", "description", "dependency"]
    assert_snapshot(
        "state_machine.json",
        {
            "turn": turn,
            "lifecycle": lifecycle,
            "workflow_statuses": [status.value for status in WorkflowRunStatus],
            "workflow_sources": {reason: source_from_reason(reason).value for reason in reasons},
        },
    )
