"""Presentation-agnostic Agent semantic events."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class AgentEventKind(str, Enum):
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    COMPACTION_COMPLETED = "compaction_completed"


class AgentEvent(BaseModel):
    kind: AgentEventKind
    message: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)
