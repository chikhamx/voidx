"""Agent thread identity, mutable state, and lifecycle descriptors."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LifecycleState(str, Enum):
    CREATED = "created"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    NEEDS_USER = "needs_user"
    RETRY_WAIT = "retry_wait"
    BLOCKED = "blocked"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_LIFECYCLES = frozenset(
    {LifecycleState.COMPLETED, LifecycleState.FAILED, LifecycleState.CANCELLED}
)


class GoalStatePatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_count: int | None = Field(default=None, ge=0)
    evaluator_failure_count: int | None = Field(default=None, ge=0)
    last_progress_key: str | None = None
    repeated_progress_count: int | None = Field(default=None, ge=0)
    last_evaluator_summary: str | None = None
    last_evaluator_next_hint: str | None = None
    last_evaluator_missing: tuple[str, ...] | None = None
    blocked_reason: str | None = None
    active: bool | None = None


class DecisionMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    goal_state_patch: GoalStatePatch | None = None
    evidence_summary: dict[str, Any] | None = None


class RuntimeDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Literal["continue", "completed", "blocked", "needs_user", "failed", "stop"]
    summary: str
    progress: Literal["none", "partial", "meaningful"] = "none"
    next_delay_seconds: float | None = None
    reason: str = ""
    metadata: DecisionMetadata | None = None


class AgentThread(BaseModel):
    model_config = ConfigDict(frozen=True)

    thread_id: str
    session_id: str | None = None
    parent_thread_id: str | None = None
    workspace: str = ""
    lifecycle: LifecycleState = LifecycleState.CREATED

    @field_validator("thread_id", "session_id", "parent_thread_id")
    @classmethod
    def require_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty")
        return value


class AgentThreadState(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    thread_id: str
    lifecycle: LifecycleState = LifecycleState.CREATED
    lifecycle_decision: RuntimeDecision | None = None
    transcript: dict[str, Any] = Field(default_factory=dict)
    goal: dict[str, Any] | None = None
    workflow: dict[str, Any] = Field(default_factory=dict)
    todo: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    compaction: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, Any] = Field(default_factory=dict)
    runtime_guards: dict[str, Any] = Field(default_factory=dict)


class ThreadAttempt(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str
    thread_id: str
    source_outbox_id: str
    state_version: int
    fencing_token: int
    lease_owner: str
    status: str
    side_effect_started: bool = False


class RuntimeOutboxItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    outbox_id: str
    thread_id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    expected_state_version: int


def apply_lifecycle_decision(
    current: LifecycleState, decision: RuntimeDecision
) -> LifecycleState:
    if current in TERMINAL_LIFECYCLES:
        raise ValueError(f"terminal lifecycle state cannot transition: {current.value}")
    if current is LifecycleState.CANCELLING:
        return LifecycleState.CANCELLED
    return {
        "continue": LifecycleState.WAITING,
        "completed": LifecycleState.COMPLETED,
        "blocked": LifecycleState.BLOCKED,
        "needs_user": LifecycleState.NEEDS_USER,
        "failed": LifecycleState.FAILED,
        "stop": LifecycleState.CANCELLED,
    }[decision.outcome]
