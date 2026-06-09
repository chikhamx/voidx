"""Structured runtime state for workflow skill orchestration."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from voidx.skills.context import skill_body_hash

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


class SkillStateEventKind(str, Enum):
    SATISFIED = "satisfied"
    BLOCKED = "blocked"
    UNBLOCKED = "unblocked"
    SKIPPED = "skipped"


class SkillEvidence(BaseModel):
    kind: str
    ref: str
    ok: bool | None = None
    summary: str = ""


class SkillStateEvent(BaseModel):
    skill: str
    kind: SkillStateEventKind
    ref: str = ""
    ok: bool | None = None
    summary: str = ""
    reason: str = ""


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
    # Intentionally excluded from serialization; only used at construction time
    # to compute body_hash. After round-tripping through SQLite/JSON, body_hash
    # survives but skill_body is lost — this is by design.
    skill_body: str = Field(default="", exclude=True)
    body_hash: str = ""
    transition_to: list[str] = Field(default_factory=list)

    @classmethod
    def from_match(
        cls,
        match: "SkillMatch",
        *,
        phase: str = "",
        scope: str = "",
        turn_count: int = 0,
        status: SkillRunStatus = SkillRunStatus.ACTIVE,
        skill_body: str | None = None,
        body_hash: str = "",
    ) -> "SkillRunState":
        body = match.skill.body if skill_body is None else skill_body
        return cls(
            name=match.name,
            status=status,
            source=source_from_reason(match.reason),
            reason=match.reason,
            phase=phase,
            scope=scope,
            activated_turn=turn_count,
            updated_turn=turn_count,
            skill_body=body,
            body_hash=body_hash or (skill_body_hash(body) if body else ""),
            transition_to=list(_workflow_skill_transitions(match.name)),
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
        if self.body_hash:
            parts.append(f"body_hash={self.body_hash}")
        if self.transition_to:
            parts.append(f"next={','.join(self.transition_to)}")
        return " ".join(parts)


def advance_skill_states(
    runs: Iterable[SkillRunState | dict[str, object]],
    events: Iterable[SkillStateEvent | dict[str, object]],
    *,
    turn_count: int = 0,
) -> list[SkillRunState]:
    states: dict[str, SkillRunState] = {}
    for item in runs:
        run = item if isinstance(item, SkillRunState) else SkillRunState.model_validate(item)
        run = run.model_copy(deep=True)
        _ensure_transition_metadata(run)
        key = _skill_key(run.name)
        if key:
            states[key] = run

    for raw_event in events:
        event = (
            raw_event
            if isinstance(raw_event, SkillStateEvent)
            else SkillStateEvent.model_validate(raw_event)
        )
        key = _skill_key(event.skill)
        if not key:
            continue
        run = states.get(key)
        if run is None:
            if event.kind == SkillStateEventKind.UNBLOCKED:
                continue
            status = _initial_status_for_event(event.kind)
            run = SkillRunState(
                name=key,
                status=status,
                source=SkillActivationSource.MANUAL,
                reason=event.reason or f"event:{event.kind.value}",
                activated_turn=turn_count,
                updated_turn=turn_count,
            )
            _ensure_transition_metadata(run)
            states[key] = run

        run.evidence.append(
            SkillEvidence(
                kind=event.kind.value,
                ref=event.ref,
                ok=event.ok,
                summary=event.summary,
            )
        )
        run.updated_turn = turn_count

        if event.kind == SkillStateEventKind.SATISFIED:
            if run.status in {
                SkillRunStatus.PENDING,
                SkillRunStatus.BLOCKED,
                SkillRunStatus.SKIPPED,
            }:
                continue
            run.status = SkillRunStatus.SATISFIED
            run.blocked_reason = ""
            _activate_transition_targets(states, run, turn_count=turn_count)
        elif event.kind == SkillStateEventKind.BLOCKED:
            run.status = SkillRunStatus.BLOCKED
            run.blocked_reason = event.reason or event.summary
        elif event.kind == SkillStateEventKind.UNBLOCKED:
            if run.status == SkillRunStatus.BLOCKED:
                run.status = SkillRunStatus.ACTIVE
                run.blocked_reason = ""
        elif event.kind == SkillStateEventKind.SKIPPED:
            run.status = SkillRunStatus.SKIPPED
            run.blocked_reason = ""

    return list(states.values())


def source_from_reason(reason: str) -> SkillActivationSource:
    if reason == "explicit":
        return SkillActivationSource.EXPLICIT
    if reason.startswith("trigger:") or reason in {"name", "description"}:
        return SkillActivationSource.TRIGGER
    return SkillActivationSource.WORKFLOW


def _activate_transition_targets(
    states: dict[str, SkillRunState],
    run: SkillRunState,
    *,
    turn_count: int,
) -> None:
    for target in run.transition_to:
        key = _skill_key(target)
        if not key:
            continue
        existing = states.get(key)
        # BLOCKED/SKIPPED successors require an explicit unblocked/reactivation
        # event before they can participate in later transitions.
        if existing is not None:
            continue
        states[key] = SkillRunState(
            name=key,
            status=SkillRunStatus.ACTIVE,
            source=SkillActivationSource.TRANSITION,
            reason=f"transition from {run.name}",
            phase=run.phase,
            scope=run.scope,
            activated_turn=turn_count,
            updated_turn=turn_count,
            transition_to=list(_workflow_skill_transitions(key)),
        )


def _ensure_transition_metadata(run: SkillRunState) -> None:
    if not run.transition_to:
        run.transition_to = list(_workflow_skill_transitions(run.name))


def _skill_key(name: str) -> str:
    return name.strip().lower()


def _workflow_skill_transitions(name: str) -> tuple[str, ...]:
    from voidx.skills.policy import workflow_skill_transitions

    return workflow_skill_transitions(name)


def _initial_status_for_event(kind: SkillStateEventKind) -> SkillRunStatus:
    if kind == SkillStateEventKind.BLOCKED:
        return SkillRunStatus.BLOCKED
    if kind == SkillStateEventKind.SKIPPED:
        return SkillRunStatus.SKIPPED
    if kind == SkillStateEventKind.SATISFIED:
        return SkillRunStatus.PENDING
    return SkillRunStatus.ACTIVE
