from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


AgentRunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
UserMessageType = Literal["message", "question", "answer", "progress", "result"]
LifecycleMessageType = Literal["completed", "failed", "cancelled"]
AgentMessageType = Literal[
    "message",
    "question",
    "answer",
    "progress",
    "result",
    "completed",
    "failed",
    "cancelled",
]
USER_MESSAGE_TYPES: frozenset[str] = frozenset({"message", "question", "answer", "progress", "result"})
AgentType = Literal["root", "sub"]


class AgentMessage(BaseModel):
    message_id: str
    session_id: str
    source_run_id: str
    target_run_id: str
    type: AgentMessageType
    payload: dict[str, Any]
    created_at: float


class AgentRun(BaseModel):
    run_id: str
    session_id: str
    parent_run_id: str
    agent_type: AgentType
    agent_name: str
    description: str
    status: AgentRunStatus
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float
    updated_at: float
