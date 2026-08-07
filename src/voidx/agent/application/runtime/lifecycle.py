"""Runtime lifecycle policy and decision normalization."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from voidx.agent.domain.thread import RuntimeDecision


class ContinuationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_delay_seconds: float = Field(default=0, ge=0)
    max_delay_seconds: float = Field(default=3600, ge=0)
    default_delay_seconds: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> "ContinuationPolicy":
        if self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("max_delay_seconds must be >= min_delay_seconds")
        return self


class LifecycleController:
    def __init__(self, policy: ContinuationPolicy | None = None) -> None:
        self._policy = policy or ContinuationPolicy()

    def normalize_decision(self, decision: RuntimeDecision) -> RuntimeDecision:
        if decision.outcome != "continue":
            return decision.model_copy(update={"next_delay_seconds": None})
        requested = (
            self._policy.default_delay_seconds
            if decision.next_delay_seconds is None
            else decision.next_delay_seconds
        )
        clamped = min(
            self._policy.max_delay_seconds,
            max(self._policy.min_delay_seconds, float(requested)),
        )
        return decision.model_copy(update={"next_delay_seconds": clamped})
