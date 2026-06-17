"""Per-turn workflow state reconciliation."""

from __future__ import annotations

from voidx.runtime.task_state import GoalResolution, GoalType, TaskState
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.policy import workflow_sort_key
from voidx.workflow.runtime import advance_workflow_states
from voidx.workflow.types import (
    WorkflowActivationSource,
    WorkflowEvidence,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
)


OVERRIDEABLE_PRECURSORS = {"brainstorm", "plan", "design"}
OVERRIDE_TARGETS = {"plan", "tdd"}
SUPERSEDED_BY_INTENT = "superseded_by_intent"


def reconcile_workflow_runs_for_turn(
    *,
    goal_resolution: GoalResolution,
    after_state: TaskState,
    turn_count: int = 0,
) -> list[WorkflowRunState]:
    runs = [run.model_copy(deep=True) for run in (after_state.workflow_runs or {}).values()]
    target = _route_target(goal_resolution)
    override = _resolve_intent_override(
        goal_resolution=goal_resolution,
        after_state=after_state,
        runs=runs,
        target=target,
        turn_count=turn_count,
    )
    if override is not None:
        return override
    if target and target in DEFAULT_WORKFLOW_DAG.nodes and _has_active(runs, target):
        compacted = _satisfy_other_active_runs(
            runs,
            target=target,
            turn_count=turn_count,
        )
        if compacted is not None:
            return compacted
    if not target and not any(run.status == WorkflowRunStatus.ACTIVE for run in runs):
        return []
    events = _reconcile_events(
        goal_resolution=goal_resolution,
        runs=runs,
        target=target,
    )
    if not events:
        activated = _activate_initial_start(
            goal_resolution=goal_resolution,
            after_state=after_state,
            runs=runs,
            target=target,
            turn_count=turn_count,
        )
        if activated is not None:
            return activated
        return runs
    return advance_workflow_states(runs, events, turn_count=turn_count)


def _resolve_intent_override(
    *,
    goal_resolution: GoalResolution,
    after_state: TaskState,
    runs: list[WorkflowRunState],
    target: str,
    turn_count: int,
) -> list[WorkflowRunState] | None:
    if target not in OVERRIDE_TARGETS or target not in DEFAULT_WORKFLOW_DAG.nodes:
        return None
    if _has_active(runs, target):
        return None
    if not _has_explicit_write_intent(goal_resolution, after_state):
        return None

    active_precursors = [
        run
        for run in sorted(runs, key=lambda item: workflow_sort_key(item.name))
        if run.status == WorkflowRunStatus.ACTIVE
        and run.name in OVERRIDEABLE_PRECURSORS
        and run.name != target
    ]
    if not active_precursors:
        return None

    updated = [run.model_copy(deep=True) for run in runs]
    by_name = {run.name: run for run in updated}
    superseded_names: list[str] = []
    for precursor in active_precursors:
        run = by_name.get(precursor.name)
        if run is None:
            continue
        superseded_names.append(run.name)
        run.status = WorkflowRunStatus.SATISFIED
        run.updated_turn = turn_count
        run.blocked_reason = ""
        run.evidence.append(
            WorkflowEvidence(
                kind=WorkflowStateEventKind.SATISFIED.value,
                ref=f"auto:turn_reconcile:{run.name}_superseded_by_{target}",
                ok=True,
                summary=(
                    "User intent explicitly selected target workflow; "
                    "stale precursor was skipped."
                ),
                condition=SUPERSEDED_BY_INTENT,
            )
        )

    if not superseded_names:
        return None

    existing = by_name.get(target)
    if existing is None:
        source = superseded_names[0]
        template = by_name[source]
        updated.append(
            WorkflowRunState(
                name=target,
                status=WorkflowRunStatus.ACTIVE,
                source=WorkflowActivationSource.TRANSITION,
                reason=f"intent override from {source}",
                goal_type=template.goal_type,
                scope=template.scope,
                personas=list(_workflow_personas(target)),
                activated_turn=turn_count,
                updated_turn=turn_count,
                transition_to=list(_workflow_transitions(target)),
            )
        )
    elif existing.status != WorkflowRunStatus.ACTIVE:
        existing.status = WorkflowRunStatus.ACTIVE
        existing.source = WorkflowActivationSource.TRANSITION
        existing.reason = f"intent override from {superseded_names[0]}"
        existing.personas = list(_workflow_personas(target))
        existing.activated_turn = turn_count
        existing.updated_turn = turn_count
        existing.transition_to = list(_workflow_transitions(target))
        existing.blocked_reason = ""

    return updated


def _has_explicit_write_intent(
    goal_resolution: GoalResolution,
    after_state: TaskState,
) -> bool:
    plan = goal_resolution.plan
    if plan is not None and plan.join:
        return plan.join in {"tdd", "debug", "feedback"}
    return False


def _satisfy_other_active_runs(
    runs: list[WorkflowRunState],
    *,
    target: str,
    turn_count: int,
) -> list[WorkflowRunState] | None:
    others = [
        run for run in runs
        if run.status == WorkflowRunStatus.ACTIVE and run.name != target
    ]
    if not others:
        return None

    updated = [run.model_copy(deep=True) for run in runs]
    for run in updated:
        if run.status != WorkflowRunStatus.ACTIVE or run.name == target:
            continue
        run.status = WorkflowRunStatus.SATISFIED
        run.updated_turn = turn_count
        run.blocked_reason = ""
        run.evidence.append(
            WorkflowEvidence(
                kind=WorkflowStateEventKind.SATISFIED.value,
                ref=f"auto:turn_reconcile:{run.name}_superseded_by_active_{target}",
                ok=True,
                summary=(
                    "Resolver selected an already-active target workflow; "
                    "stale active workflow was closed."
                ),
                condition="superseded_by_active_target",
            )
        )
    return updated


def _reconcile_events(
    *,
    goal_resolution: GoalResolution,
    runs: list[WorkflowRunState],
    target: str,
) -> list[WorkflowStateEvent]:
    del goal_resolution
    if not target:
        return []
    event = _resolve_auto_transition(runs, target)
    return [event] if event is not None else []


def _resolve_auto_transition(
    active_runs: list[WorkflowRunState],
    target: str,
) -> WorkflowStateEvent | None:
    if target not in DEFAULT_WORKFLOW_DAG.nodes:
        return None
    if _has_active(active_runs, target):
        return None
    for run in sorted(active_runs, key=lambda item: workflow_sort_key(item.name)):
        if run.status != WorkflowRunStatus.ACTIVE:
            continue
        for edge in DEFAULT_WORKFLOW_DAG.edges_from(run.name):
            if edge.target == target:
                return WorkflowStateEvent(
                    workflow=run.name,
                    kind=WorkflowStateEventKind.SATISFIED,
                    ref=f"auto:turn_reconcile:{run.name}_to_{target}",
                    ok=True,
                    summary=f"User intent implies transition from {run.name} to {target}.",
                    reason=f"plan.join={target} from goal resolver",
                    condition=edge.condition,
                )
    return None


def _activate_initial_start(
    *,
    goal_resolution: GoalResolution,
    after_state: TaskState,
    runs: list[WorkflowRunState],
    target: str,
    turn_count: int,
) -> list[WorkflowRunState] | None:
    del goal_resolution
    if not target or target not in DEFAULT_WORKFLOW_DAG.nodes:
        return None
    if _has_active(runs, target):
        return None
    if any(run.status == WorkflowRunStatus.ACTIVE for run in runs):
        return None

    goal = after_state.current_goal
    updated = [run.model_copy(deep=True) for run in runs]
    updated.append(
        WorkflowRunState(
            name=target,
            status=WorkflowRunStatus.ACTIVE,
            source=WorkflowActivationSource.TRANSITION,
            reason="resolver plan.join",
            goal_type=goal.type.value if goal is not None else "",
            scope=goal.label if goal is not None else "",
            personas=list(_workflow_personas(target)),
            activated_turn=turn_count,
            updated_turn=turn_count,
            transition_to=list(_workflow_transitions(target)),
        )
    )
    return updated


def _route_target(goal_resolution: GoalResolution) -> str:
    plan = goal_resolution.plan
    if plan is None:
        return ""
    return plan.join.strip().lower()


def _has_active(runs: list[WorkflowRunState], name: str) -> bool:
    return any(run.name == name and run.status == WorkflowRunStatus.ACTIVE for run in runs)


def _workflow_transitions(name: str) -> tuple[str, ...]:
    from voidx.workflow.policy import workflow_transitions

    return workflow_transitions(name)


def _workflow_personas(name: str) -> tuple[str, ...]:
    from voidx.workflow.policy import workflow_personas

    return workflow_personas(name)


__all__ = ["reconcile_workflow_runs_for_turn"]
