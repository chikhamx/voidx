"""Structured runtime state for workflow orchestration."""

from __future__ import annotations

from collections.abc import Iterable

from collections import deque

from voidx.agent.domain.automation.workflow_policy import (
    is_workflow_terminal_condition,
    workflow_edges,
    workflow_personas,
    workflow_transitions,
)
from voidx.agent.domain.agent_profile import content_hash_of
from voidx.agent.domain.automation.workflow_schema import WorkflowDAG
from voidx.agent.domain.automation.workflow import (
    WorkflowActivationSource,
    WorkflowEvidence,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
    source_from_reason,
)


def validate_workflow_dag_hashes(
    runs: Iterable[WorkflowRunState | dict[str, object]],
    *,
    dag: WorkflowDAG,
) -> list[WorkflowRunState]:
    """Backfill legacy hashes and block states pinned to another DAG."""
    current_dag_hash = content_hash_of(dag.model_dump(mode="json"))
    validated: list[WorkflowRunState] = []
    for item in runs:
        run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        run = run.model_copy(deep=True)
        if not run.dag_hash:
            run.dag_hash = current_dag_hash
        elif run.dag_hash != current_dag_hash:
            run.status = WorkflowRunStatus.BLOCKED
            run.blocked_reason = "workflow_dag_hash_mismatch"
            if not any(evidence.kind == "dag_mismatch" for evidence in run.evidence):
                run.evidence.append(
                    WorkflowEvidence(
                        kind="dag_mismatch",
                        ref="workflow:dag_hash",
                        ok=False,
                        summary="Persisted workflow DAG hash does not match the active profile snapshot.",
                    )
                )
        validated.append(run)
    return validated


def advance_workflow_states(
    runs: Iterable[WorkflowRunState | dict[str, object]],
    events: Iterable[WorkflowStateEvent | dict[str, object]],
    *,
    dag: WorkflowDAG,
    turn_count: int = 0,
) -> list[WorkflowRunState]:
    states = {
        _workflow_key(run.name): run
        for run in validate_workflow_dag_hashes(runs, dag=dag)
        if _workflow_key(run.name)
    }
    current_dag_hash = content_hash_of(dag.model_dump(mode="json"))
    mismatched = {
        key
        for key, run in states.items()
        if run.blocked_reason == "workflow_dag_hash_mismatch"
    }
    for run in states.values():
        _ensure_transition_metadata(run, dag)

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
        if run is not None and key in mismatched:
            continue
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
                dag_hash=current_dag_hash,
            )
            _ensure_transition_metadata(run, dag)
            states[key] = run

        if event.kind == WorkflowStateEventKind.SATISFIED:
            if run.status in {
                WorkflowRunStatus.PENDING,
                WorkflowRunStatus.BLOCKED,
                WorkflowRunStatus.SATISFIED,
                WorkflowRunStatus.SKIPPED,
            }:
                continue
            if not _can_satisfy_run(run, event, dag):
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
            if is_workflow_terminal_condition(event.condition, dag):
                _cascade_skip_downstream(states, run, dag, turn_count=turn_count)
            else:
                _activate_transition_targets(
                    states,
                    run,
                    dag,
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
    dag: WorkflowDAG,
    *,
    turn_count: int,
    condition: str = "",
) -> None:
    targets = _transition_targets_for(run, dag, condition=condition)
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
            goal=run.goal,
            scope=run.scope,
            personas=list(workflow_personas(key, dag)),
            activated_turn=turn_count,
            updated_turn=turn_count,
            dag_hash=run.dag_hash,
            transition_to=list(workflow_transitions(key, dag)),
        )


def _cascade_skip_downstream(
    states: dict[str, WorkflowRunState],
    run: WorkflowRunState,
    dag: WorkflowDAG,
    *,
    turn_count: int,
) -> None:
    downstream = _reachable_downstream(run.name, dag)
    for name in downstream:
        target = states.get(_workflow_key(name))
        if target is None or target.status != WorkflowRunStatus.ACTIVE:
            continue
        if _has_other_active_precursor(states, name, dag, exclude=run.name):
            continue
        target.status = WorkflowRunStatus.SKIPPED
        target.updated_turn = turn_count
        target.blocked_reason = ""
        target.evidence.append(
            WorkflowEvidence(
                kind=WorkflowStateEventKind.SKIPPED.value,
                ref=f"cascade:upstream_{run.name}_done",
                ok=True,
                summary=f"Upstream node {run.name} exited with done; downstream skipped.",
                condition="done",
            )
        )


def _reachable_downstream(name: str, dag: WorkflowDAG) -> list[str]:
    start = _workflow_key(name)
    if not start:
        return []
    result: list[str] = []
    seen = {start}
    pending: deque[str] = deque([start])
    while pending:
        current = pending.popleft()
        for edge in workflow_edges(current, dag):
            target = _workflow_key(edge.target)
            if not target or target in seen:
                continue
            seen.add(target)
            result.append(target)
            pending.append(target)
    return result


def _has_other_active_precursor(
    states: dict[str, WorkflowRunState],
    name: str,
    dag: WorkflowDAG,
    *,
    exclude: str,
) -> bool:
    target = _workflow_key(name)
    excluded = _workflow_key(exclude)
    if not target:
        return False
    for source in list(states):
        if source == excluded:
            continue
        run = states.get(source)
        if run is None or run.status != WorkflowRunStatus.ACTIVE:
            continue
        if any(_workflow_key(edge.target) == target for edge in workflow_edges(source, dag)):
            return True
    return False


def _ensure_transition_metadata(run: WorkflowRunState, dag: WorkflowDAG) -> None:
    if not run.transition_to:
        run.transition_to = list(workflow_transitions(run.name, dag))


def _transition_targets_for(run: WorkflowRunState, dag: WorkflowDAG, *, condition: str = "") -> list[str]:
    normalized_condition = condition.strip().lower()
    if not normalized_condition:
        return []
    if is_workflow_terminal_condition(normalized_condition, dag):
        return []
    return [
        edge.target
        for edge in workflow_edges(run.name, dag)
        if edge.condition == normalized_condition
    ]


def _can_satisfy_run(run: WorkflowRunState, event: WorkflowStateEvent, dag: WorkflowDAG) -> bool:
    if not _gate_satisfied(run.name, event):
        return False
    condition = event.condition.strip().lower()
    if not condition:
        return False
    if is_workflow_terminal_condition(condition, dag):
        return True
    return any(edge.condition == condition for edge in workflow_edges(run.name, dag))


def _gate_satisfied(workflow: str, event: WorkflowStateEvent) -> bool:
    del workflow, event
    return True


def _workflow_key(name: str) -> str:
    return name.strip().lower()


def _initial_status_for_event(kind: WorkflowStateEventKind) -> WorkflowRunStatus:
    if kind == WorkflowStateEventKind.BLOCKED:
        return WorkflowRunStatus.BLOCKED
    if kind == WorkflowStateEventKind.SKIPPED:
        return WorkflowRunStatus.SKIPPED
    if kind == WorkflowStateEventKind.SATISFIED:
        return WorkflowRunStatus.PENDING
    return WorkflowRunStatus.ACTIVE
