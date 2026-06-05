"""Structured runtime state for workflow skill orchestration."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from voidx.skills.schema import SkillMatch


class SkillRunStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class SkillActivationSource(str, Enum):
    EXPLICIT = "explicit"
    WORKFLOW = "workflow"
    TRIGGER = "trigger"
    DEPENDENCY = "dependency"
    TRANSITION = "transition"
    MANUAL = "manual"


class SkillEvidence(BaseModel):
    kind: str
    ref: str
    ok: bool | None = None
    summary: str = ""


class SkillRunState(BaseModel):
    name: str
    status: SkillRunStatus = SkillRunStatus.PENDING
    source: SkillActivationSource = SkillActivationSource.WORKFLOW
    reason: str = ""
    phase: str = ""
    scope: str = ""
    activated_turn: int = 0
    updated_turn: int = 0
    evidence: list[SkillEvidence] = Field(default_factory=list)
    blocked_reason: str = ""

    @classmethod
    def from_match(
        cls,
        match: "SkillMatch",
        *,
        phase: str = "",
        scope: str = "",
        turn_count: int = 0,
        status: SkillRunStatus = SkillRunStatus.ACTIVE,
    ) -> "SkillRunState":
        return cls(
            name=match.name,
            status=status,
            source=source_from_reason(match.reason),
            reason=match.reason,
            phase=phase,
            scope=scope,
            activated_turn=turn_count,
            updated_turn=turn_count,
        )

    def state_summary(self) -> str:
        parts = [
            f"{self.name}={self.status.value}",
        ]
        if self.phase:
            parts.append(f"phase={self.phase}")
        parts.append(f"source={self.source.value}")
        if self.reason:
            parts.append(f"reason={self.reason}")
        if self.blocked_reason:
            parts.append(f"blocked={self.blocked_reason}")
        return " ".join(parts)


def source_from_reason(reason: str) -> SkillActivationSource:
    if reason == "explicit":
        return SkillActivationSource.EXPLICIT
    if reason.startswith("trigger:") or reason in {"name", "description"}:
        return SkillActivationSource.TRIGGER
    return SkillActivationSource.WORKFLOW
