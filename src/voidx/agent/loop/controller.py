"""Per-attempt lifecycle controller for runtime-backed /loop turns."""

from __future__ import annotations

from dataclasses import dataclass

from collections.abc import Mapping

from voidx.agent.domain.loop import LoopDecision, LoopMode, LoopSpec
from voidx.agent.domain.thread import RuntimeDecision


@dataclass
class LoopAttemptController:
    spec: LoopSpec
    _decision: RuntimeDecision | None = None

    async def submit_decision(self, decision: LoopDecision | RuntimeDecision | Mapping[str, object]) -> RuntimeDecision:
        runtime_decision = _to_runtime_decision(decision)
        if runtime_decision.outcome != "continue":
            # The loop only ends via /loop stop or process exit; model-submitted
            # terminal/pause outcomes are rejected so the loop cannot kill or
            # pause itself.
            raise ValueError(
                f"outcome {runtime_decision.outcome!r} is not allowed: use 'continue'"
            )
        if self.spec.mode is LoopMode.FIXED and runtime_decision.outcome == "continue":
            runtime_decision = runtime_decision.model_copy(
                update={"next_delay_seconds": self.spec.interval_seconds}
            )
        elif runtime_decision.outcome != "continue":
            runtime_decision = runtime_decision.model_copy(update={"next_delay_seconds": None})
        self._decision = runtime_decision
        return runtime_decision

    def final_decision(self) -> RuntimeDecision | None:
        return self._decision

    def spec_decision(self, **kwargs) -> LoopDecision:
        return LoopDecision(**kwargs)


def _to_runtime_decision(
    decision: LoopDecision | RuntimeDecision | Mapping[str, object]
) -> RuntimeDecision:
    if isinstance(decision, RuntimeDecision):
        return decision
    if isinstance(decision, Mapping):
        return RuntimeDecision.model_validate(dict(decision))
    return RuntimeDecision(
        outcome=decision.outcome,
        summary=decision.summary,
        progress=decision.progress,
        next_delay_seconds=decision.next_delay_seconds,
        reason=decision.reason,
    )
