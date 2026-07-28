"""AI approval request/response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AiApprovalRequestItem(BaseModel):
    id: str
    tool_name: str
    pattern: str = ""
    risk_level: Literal["dangerous", "extreme"] = "dangerous"
    risk_tags: tuple[str, ...] = ()
    risk_reason: str = ""
    args: dict = Field(default_factory=dict)
    args_sha256: str


class AiApprovalItemResult(BaseModel):
    id: str
    decision: Literal["allow", "deny"]
    reason: str = ""


class AiApprovalResponse(BaseModel):
    decisions: list[AiApprovalItemResult]


class AiApprovalResult(BaseModel):
    allowed_ids: frozenset[str] = frozenset()
    reviewed_ids: frozenset[str] = frozenset()
    denied_reasons: dict[str, str] = Field(default_factory=dict)
    skipped_reasons: dict[str, str] = Field(default_factory=dict)
    reason: Literal[
        "reviewed",
        "disabled",
        "unavailable",
        "invalid_response",
        "skipped",
        "timeout",
        "connection_error",
        "error",
    ] = "error"
