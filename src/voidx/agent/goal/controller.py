"""Attempt-scoped controller for evaluator Goal lifecycle decisions."""

from __future__ import annotations

from typing import Any

from voidx.agent.domain.thread import RuntimeDecision


class GoalController:
    def __init__(self, *, attempt_id: str = "") -> None:
        self.attempt_id = attempt_id
        self._decision: RuntimeDecision | None = None

    async def submit_decision(self, decision: dict[str, Any]) -> RuntimeDecision:
        if self._decision is not None:
            return self._decision
        outcome = str(decision.get("outcome") or "")
        if outcome not in {"completed", "continue", "blocked"}:
            raise ValueError(f"invalid goal outcome: {outcome}")
        summary = str(decision.get("summary") or "").strip()
        if not summary:
            raise ValueError("goal decision requires a summary")
        self._decision = RuntimeDecision(
            outcome=outcome,
            summary=summary,
            progress=decision.get("progress", "none"),
            next_delay_seconds=decision.get("next_delay_seconds"),
            reason=str(decision.get("reason") or ""),
        )
        return self._decision

    def final_decision(self) -> RuntimeDecision | None:
        return self._decision
