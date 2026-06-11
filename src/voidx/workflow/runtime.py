"""Structured runtime state for workflow orchestration."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from pydantic import BaseModel, Field

from voidx.workflow.context import workflow_body_hash


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
    phase: str = ""
    scope: str = ""
    activated_turn: int = 0
    updated_turn: int = 0
    evidence: list[WorkflowEvidence] = Field(default_factory=list)
    blocked_reason: str = ""
    body_hash: str = ""
    transition_to: list[str] = Field(default_factory=list)

    @classmethod
    def from_match(
        cls,
        match,
        *,
        phase: str = "",
        scope: str = "",
        turn_count: int = 0,
        status: WorkflowRunStatus = WorkflowRunStatus.ACTIVE,
        workflow_body: str | None = None,
        body_hash: str = "",
    ) -> "WorkflowRunState":
        body = match.body if workflow_body is None else workflow_body
        return cls(
            name=match.name,
            status=status,
            source=source_from_reason(match.reason),
            reason=match.reason,
            phase=phase,
            scope=scope,
            activated_turn=turn_count,
            updated_turn=turn_count,
            body_hash=body_hash or (workflow_body_hash(body) if body else ""),
            transition_to=list(_workflow_transitions(match.name)),
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


def advance_workflow_states(
    runs: Iterable[WorkflowRunState | dict[str, object]],
    events: Iterable[WorkflowStateEvent | dict[str, object]],
    *,
    turn_count: int = 0,
) -> list[WorkflowRunState]:
    states: dict[str, WorkflowRunState] = {}
    for item in runs:
        run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        run = run.model_copy(deep=True)
        _ensure_transition_metadata(run)
        key = _workflow_key(run.name)
        if key:
            states[key] = run

    for raw_event in events:
        event = (
            raw_event
            if isinstance(raw_event, WorkflowStateEvent)
            else WorkflowStateEvent.model_validate(raw_event)
        )
        key = _workflow_key(event.workflow)
        if not key:
            continue
        run = states.get(key)
        if run is None:
            if event.kind == WorkflowStateEventKind.UNBLOCKED:
                continue
            status = _initial_status_for_event(event.kind)
            run = WorkflowRunState(
                name=key,
                status=status,
                source=WorkflowActivationSource.MANUAL,
                reason=event.reason or f"event:{event.kind.value}",
                activated_turn=turn_count,
                updated_turn=turn_count,
            )
            _ensure_transition_metadata(run)
            states[key] = run

        run.evidence.append(
            WorkflowEvidence(
                kind=event.kind.value,
                ref=event.ref,
                ok=event.ok,
                summary=event.summary,
                condition=event.condition,
            )
        )
        run.updated_turn = turn_count

        if event.kind == WorkflowStateEventKind.SATISFIED:
            if run.status in {
                WorkflowRunStatus.PENDING,
                WorkflowRunStatus.BLOCKED,
                WorkflowRunStatus.SKIPPED,
            }:
                continue
            run.status = WorkflowRunStatus.SATISFIED
            run.blocked_reason = ""
            _activate_transition_targets(
                states,
                run,
                turn_count=turn_count,
                condition=event.condition,
            )
        elif event.kind == WorkflowStateEventKind.BLOCKED:
            run.status = WorkflowRunStatus.BLOCKED
            run.blocked_reason = event.reason or event.summary
        elif event.kind == WorkflowStateEventKind.UNBLOCKED:
            if run.status == WorkflowRunStatus.BLOCKED:
                run.status = WorkflowRunStatus.ACTIVE
                run.blocked_reason = ""
        elif event.kind == WorkflowStateEventKind.SKIPPED:
            run.status = WorkflowRunStatus.SKIPPED
            run.blocked_reason = ""

    return list(states.values())


def source_from_reason(reason: str) -> WorkflowActivationSource:
    if reason == "explicit":
        return WorkflowActivationSource.EXPLICIT
    if reason.startswith("trigger:") or reason in {"name", "description"}:
        return WorkflowActivationSource.TRIGGER
    return WorkflowActivationSource.WORKFLOW


def _activate_transition_targets(
    states: dict[str, WorkflowRunState],
    run: WorkflowRunState,
    *,
    turn_count: int,
    condition: str = "",
) -> None:
    targets = _transition_targets_for(run, condition=condition)
    for target in targets:
        key = _workflow_key(target)
        if not key:
            continue
        existing = states.get(key)
        if existing is not None:
            continue
        states[key] = WorkflowRunState(
            name=key,
            status=WorkflowRunStatus.ACTIVE,
            source=WorkflowActivationSource.TRANSITION,
            reason=(
                f"transition from {run.name} via {condition}"
                if condition
                else f"transition from {run.name}"
            ),
            phase=run.phase,
            scope=run.scope,
            activated_turn=turn_count,
            updated_turn=turn_count,
            transition_to=list(_workflow_transitions(key)),
        )


def _ensure_transition_metadata(run: WorkflowRunState) -> None:
    if not run.transition_to:
        run.transition_to = list(_workflow_transitions(run.name))


def _transition_targets_for(run: WorkflowRunState, *, condition: str = "") -> list[str]:
    normalized_condition = condition.strip()
    from voidx.workflow.policy import is_workflow_terminal_condition

    if is_workflow_terminal_condition(normalized_condition):
        return []
    if normalized_condition:
        return [
            edge.target
            for edge in _workflow_edges(run.name)
            if edge.condition == normalized_condition
        ]
    if len(run.transition_to) == 1:
        return list(run.transition_to)
    return []


def _workflow_key(name: str) -> str:
    return name.strip().lower()


def _workflow_transitions(name: str) -> tuple[str, ...]:
    from voidx.workflow.policy import workflow_transitions

    return workflow_transitions(name)


def _workflow_edges(name: str):
    from voidx.workflow.policy import workflow_edges

    return workflow_edges(name)


def _initial_status_for_event(kind: WorkflowStateEventKind) -> WorkflowRunStatus:
    if kind == WorkflowStateEventKind.BLOCKED:
        return WorkflowRunStatus.BLOCKED
    if kind == WorkflowStateEventKind.SKIPPED:
        return WorkflowRunStatus.SKIPPED
    if kind == WorkflowStateEventKind.SATISFIED:
        return WorkflowRunStatus.PENDING
    return WorkflowRunStatus.ACTIVE
