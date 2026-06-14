"""Structured runtime state for workflow orchestration."""

from __future__ import annotations

from collections.abc import Iterable

from voidx.workflow.types import (
    WorkflowActivationSource,
    WorkflowEvidence,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
    source_from_reason,
)


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

        if event.kind == WorkflowStateEventKind.SATISFIED:
            if run.status in {
                WorkflowRunStatus.PENDING,
                WorkflowRunStatus.BLOCKED,
                WorkflowRunStatus.SATISFIED,
                WorkflowRunStatus.SKIPPED,
            }:
                continue
            if not _can_satisfy_run(run, event):
                continue
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
            goal_type=run.goal_type,
            scope=run.scope,
            personas=list(_workflow_personas(key)),
            activated_turn=turn_count,
            updated_turn=turn_count,
            transition_to=list(_workflow_transitions(key)),
        )


def _ensure_transition_metadata(run: WorkflowRunState) -> None:
    if not run.transition_to:
        run.transition_to = list(_workflow_transitions(run.name))


def _transition_targets_for(run: WorkflowRunState, *, condition: str = "") -> list[str]:
    normalized_condition = condition.strip().lower()
    from voidx.workflow.policy import is_workflow_terminal_condition

    if not normalized_condition:
        return []
    if is_workflow_terminal_condition(normalized_condition):
        return []
    return [
        edge.target
        for edge in _workflow_edges(run.name)
        if edge.condition == normalized_condition
    ]


def _can_satisfy_run(run: WorkflowRunState, event: WorkflowStateEvent) -> bool:
    if not _gate_satisfied(run.name, event):
        return False
    condition = event.condition.strip().lower()
    if not condition:
        return False
    from voidx.workflow.policy import is_workflow_terminal_condition

    if is_workflow_terminal_condition(condition):
        return True
    return any(edge.condition == condition for edge in _workflow_edges(run.name))


def _gate_satisfied(workflow: str, event: WorkflowStateEvent) -> bool:
    gate = _workflow_gate(workflow)
    if gate is None:
        return True
    if not gate.required_before_transition and not gate.description:
        return True
    return bool(event.reason.strip())


def _workflow_key(name: str) -> str:
    return name.strip().lower()


def _workflow_transitions(name: str) -> tuple[str, ...]:
    from voidx.workflow.policy import workflow_transitions

    return workflow_transitions(name)


def _workflow_edges(name: str):
    from voidx.workflow.policy import workflow_edges

    return workflow_edges(name)


def _workflow_gate(name: str):
    from voidx.workflow.policy import workflow_gate

    return workflow_gate(name)


def _workflow_personas(name: str) -> tuple[str, ...]:
    from voidx.workflow.policy import workflow_personas

    return workflow_personas(name)


def _initial_status_for_event(kind: WorkflowStateEventKind) -> WorkflowRunStatus:
    if kind == WorkflowStateEventKind.BLOCKED:
        return WorkflowRunStatus.BLOCKED
    if kind == WorkflowStateEventKind.SKIPPED:
        return WorkflowRunStatus.SKIPPED
    if kind == WorkflowStateEventKind.SATISFIED:
        return WorkflowRunStatus.PENDING
    return WorkflowRunStatus.ACTIVE
