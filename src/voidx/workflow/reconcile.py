"""Per-turn workflow state reconciliation."""

from __future__ import annotations

from voidx.runtime.task_state import GoalResolution, TaskState
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.policy import workflow_sort_key
from voidx.workflow.runtime import advance_workflow_states
from voidx.workflow.types import (
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
)


def reconcile_workflow_runs_for_turn(
    *,
    goal_resolution: GoalResolution,
    after_state: TaskState,
    turn_count: int = 0,
) -> list[WorkflowRunState]:
    runs = [run.model_copy(deep=True) for run in (after_state.workflow_runs or {}).values()]
    events = _reconcile_events(
        goal_resolution=goal_resolution,
        runs=runs,
    )
    if not events:
        return runs
    return advance_workflow_states(runs, events, turn_count=turn_count)


def _reconcile_events(
    *,
    goal_resolution: GoalResolution,
    runs: list[WorkflowRunState],
) -> list[WorkflowStateEvent]:
    next_workflow = goal_resolution.next_workflow
    if not next_workflow:
        return []
    event = _resolve_auto_transition(runs, next_workflow)
    return [event] if event is not None else []


def _resolve_auto_transition(
    active_runs: list[WorkflowRunState],
    next_workflow: str,
) -> WorkflowStateEvent | None:
    if next_workflow not in DEFAULT_WORKFLOW_DAG.nodes:
        return None
    if _has_active(active_runs, next_workflow):
        return None
    for run in sorted(active_runs, key=lambda item: workflow_sort_key(item.name)):
        if run.status != WorkflowRunStatus.ACTIVE:
            continue
        for edge in DEFAULT_WORKFLOW_DAG.edges_from(run.name):
            if edge.target == next_workflow:
                return WorkflowStateEvent(
                    workflow=run.name,
                    kind=WorkflowStateEventKind.SATISFIED,
                    ref=f"auto:turn_reconcile:{run.name}_to_{next_workflow}",
                    ok=True,
                    summary=f"User intent implies transition from {run.name} to {next_workflow}.",
                    reason=f"next_workflow={next_workflow} from goal resolver",
                    condition=edge.condition,
                )
    return None


def _has_active(runs: list[WorkflowRunState], name: str) -> bool:
    return any(run.name == name and run.status == WorkflowRunStatus.ACTIVE for run in runs)


__all__ = ["reconcile_workflow_runs_for_turn"]
