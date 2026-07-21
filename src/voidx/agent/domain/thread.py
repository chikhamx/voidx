"""Agent thread identity and lifecycle descriptors."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator


class LifecycleState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentThread(BaseModel):
    model_config = ConfigDict(frozen=True)

    thread_id: str
    session_id: str | None = None
    parent_thread_id: str | None = None
    lifecycle: LifecycleState = LifecycleState.CREATED

    @field_validator("thread_id", "session_id", "parent_thread_id")
    @classmethod
    def require_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty")
        return value
