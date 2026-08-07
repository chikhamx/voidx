"""Workflow runtime data types."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class WorkflowRoute(BaseModel):
    join: str = ""
    leave: str | None = None


class WorkflowRunStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class WorkflowActivationSource(str, Enum):
    EXPLICIT = "explicit"
    WORKFLOW = "workflow"
    TRIGGER = "trigger"
    DEPENDENCY = "dependency"
    TRANSITION = "transition"
    MANUAL = "manual"


class WorkflowStateEventKind(str, Enum):
    ACTIVATED = "activated"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"
    UNBLOCKED = "unblocked"
    SKIPPED = "skipped"


class WorkflowEvidence(BaseModel):
    kind: str
    ref: str
    ok: bool | None = None
    summary: str = ""
    condition: str = ""


class WorkflowStateEvent(BaseModel):
    workflow: str
    kind: WorkflowStateEventKind
    ref: str = ""
    ok: bool | None = None
    summary: str = ""
    reason: str = ""
    condition: str = ""


class WorkflowRunState(BaseModel):
    name: str
    status: WorkflowRunStatus = WorkflowRunStatus.PENDING
    source: WorkflowActivationSource = WorkflowActivationSource.WORKFLOW
    reason: str = ""
    goal_type: str = ""
    goal: str = ""
    scope: str = ""
    personas: list[str] = Field(default_factory=list)
    activated_turn: int = 0
    updated_turn: int = 0
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    blocked_reason: str = ""
    body_hash: str = ""
    transition_to: list[str] = Field(default_factory=list)

    def state_summary(self) -> str:
        parts = [
            f"{self.name}={self.status.value}",
        ]
        if self.goal_type:
            parts.append(f"goal_type={self.goal_type}")
        if self.goal:
            parts.append(f"goal={self.goal}")
        parts.append(f"source={self.source.value}")
        if self.reason:
            parts.append(f"reason={self.reason}")
        if self.blocked_reason:
            parts.append(f"blocked={self.blocked_reason}")
        if self.body_hash:
            parts.append(f"body_hash={self.body_hash}")
        if self.transition_to:
            parts.append(f"next={','.join(self.transition_to)}")
        return " ".join(parts)


def source_from_reason(reason: str) -> WorkflowActivationSource:
    if reason == "explicit":
        return WorkflowActivationSource.EXPLICIT
    if reason.startswith("trigger:") or reason in {"name", "description"}:
        return WorkflowActivationSource.TRIGGER
    return WorkflowActivationSource.WORKFLOW

__all__ = [
    "WorkflowRoute",
    "WorkflowActivationSource",
    "WorkflowEvidence",
    "WorkflowRunState",
    "WorkflowRunStatus",
    "WorkflowStateEvent",
    "WorkflowStateEventKind",
    "source_from_reason",
]
