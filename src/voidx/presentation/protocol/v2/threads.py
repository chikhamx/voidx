"""Thread / Turn / Item primitives for protocol v2.

- Thread  = one agent session (maps to session_id)
- Turn    = one submit → agent execution → turn.completed cycle
- Item    = atomic event within a Turn, with started → delta → completed lifecycle
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ThreadInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    thread_id: str
    title: str = ""
    workspace: str = "."
    directory: str = ""
    model_provider: str = ""
    model_name: str = ""
    status: Literal["idle", "running", "waiting_for_user", "waiting_for_write_lock", "cancelling", "failed"] = "idle"
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    runtime_profile: str = "coding"


class TurnInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn_id: str
    thread_id: str
    status: Literal["running", "completed", "cancelled", "failed"] = "running"
    started_at: float = 0
    elapsed: float | None = None


class Item(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    turn_id: str
    thread_id: str
    kind: Literal[
        "message",
        "assistant_stream",
        "tool",
        "todo",
        "subagent",
        "status",
        "prompt",
    ]
    lifecycle: Literal["started", "delta", "completed"] = "started"
    data: dict[str, Any] = Field(default_factory=dict)
