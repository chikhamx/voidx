"""Subagent run, message, routing, and terminal-state policy."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


AgentRunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
AgentWaitOutcome = Literal[
    "already_terminal",
    "terminal_reached_during_wait",
    "timed_out_still_running",
]
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
TERMINAL_STATUSES: frozenset[AgentRunStatus] = frozenset({"completed", "failed", "cancelled"})
AgentType = Literal["root", "sub"]


class AgentGatewayError(ValueError):
    def __init__(self, message: str, *, reason: str = "gateway_error") -> None:
        super().__init__(message)
        self.reason = reason


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
    wait_outcome: AgentWaitOutcome | None = None


def ensure_send_route(source: AgentRun, target: AgentRun) -> None:
    _ensure_same_session(source, target)
    if source.agent_type == "root" and target.parent_run_id == source.run_id:
        return
    if target.run_id == source.parent_run_id:
        return
    raise AgentGatewayError("Route not allowed", reason="route_not_allowed")


def ensure_control_route(source: AgentRun, target: AgentRun) -> None:
    _ensure_same_session(source, target)
    if source.agent_type == "root" and target.parent_run_id == source.run_id:
        return
    raise AgentGatewayError("Route not allowed", reason="route_not_allowed")


def ensure_open_send(source: AgentRun, target: AgentRun) -> None:
    if source.status in TERMINAL_STATUSES:
        raise AgentGatewayError("Source run is terminal")
    if target.status in TERMINAL_STATUSES:
        raise AgentGatewayError("Target run is terminal")


def finish_run(
    run: AgentRun,
    *,
    status: AgentRunStatus,
    result: dict[str, Any] | str | None = None,
    error: str | None = None,
    now: float,
) -> AgentRun:
    if run.status in TERMINAL_STATUSES:
        return run
    result_payload = _result_payload(result) if result is not None else None
    return run.model_copy(
        update={
            "status": status,
            "result": result_payload,
            "error": error,
            "updated_at": now,
        }
    )


def _ensure_same_session(source: AgentRun, target: AgentRun) -> None:
    if source.session_id != target.session_id:
        raise AgentGatewayError(
            "Runs must belong to the same session",
            reason="cross_session",
        )


def _result_payload(result: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    return {"result": result}
