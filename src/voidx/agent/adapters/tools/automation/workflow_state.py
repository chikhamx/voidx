from __future__ import annotations

from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext
from voidx.agent.application.automation.workflow.service import (
    WorkflowService, workflow_edges, workflow_sort_key, workflow_transitions,
)
from voidx.agent.domain.automation.workflow import (
    WorkflowActivationSource, WorkflowEvidence, WorkflowRunState,
    WorkflowRunStatus, WorkflowStateEventKind,
)

def _current_runs(ctx: ToolContext) -> list[WorkflowRunState]:
    runs_by_name: dict[str, WorkflowRunState] = {}
    for item in ctx.runtime.workflow_runs:
        run = item.model_copy(deep=True)
        key = run.name.strip().lower()
        if not key:
            continue
        runs_by_name[key] = run
    runs = list(runs_by_name.values())
    if runs:
        return runs
    legacy_names: dict[str, str] = {}
    for name in ctx.runtime.active_workflow_names:
        normalized = name.strip().lower()
        if normalized:
            legacy_names[normalized] = normalized
    return [
        WorkflowRunState(name=name, status=WorkflowRunStatus.ACTIVE)
        for name in legacy_names.values()
    ]


def _active_runs(runs: list[WorkflowRunState]) -> list[WorkflowRunState]:
    active = [run for run in runs if run.status == WorkflowRunStatus.ACTIVE]
    return sorted(active, key=lambda run: workflow_sort_key(run.name))


def _effective_goal(input_goal: str, selected: WorkflowRunState, ctx: ToolContext) -> tuple[str, str]:
    if input_goal:
        return input_goal, "input"
    run_goal = selected.goal.strip()
    if run_goal:
        return run_goal, "run"
    current_goal = ctx.runtime.goal_target.strip()
    if current_goal:
        return current_goal, "current_goal"
    return "", ""


def _apply_goal_to_runs(runs: list[WorkflowRunState], names: list[str], goal: str) -> list[WorkflowRunState]:
    if not names:
        return runs
    targets = {name.strip().lower() for name in names}
    updated = [run.model_copy(deep=True) for run in runs]
    for run in updated:
        if run.name.strip().lower() in targets:
            run.goal = goal
    return updated

def _activate_node(
    runs: list[WorkflowRunState],
    node_name: str,
    *,
    goal: str = "",
) -> list[WorkflowRunState]:
    updated = [run.model_copy(deep=True) for run in runs]
    node = WorkflowService().get(node_name)
    personas = [node.persona] if node is not None and node.persona else []
    normalized_name = node_name.strip().lower()
    existing = next((run for run in updated if run.name.strip().lower() == normalized_name), None)
    if existing is None:
        existing = WorkflowRunState(name=node_name)
        updated.append(existing)
    existing.status = WorkflowRunStatus.ACTIVE
    existing.source = WorkflowActivationSource.MANUAL
    existing.reason = "manual:enter"
    existing.goal_type = ""
    existing.goal = goal
    existing.scope = ""
    existing.personas = personas
    existing.blocked_reason = ""
    existing.transition_to = list(workflow_transitions(node_name))
    existing.evidence.append(
        WorkflowEvidence(
            kind=WorkflowStateEventKind.ACTIVATED.value,
            ref="tool:workflow",
            ok=True,
            summary=goal or "Manual workflow activation.",
            condition="enter",
        )
    )
    return updated


def _satisfy_active_runs(
    runs: list[WorkflowRunState],
    names: list[str],
    *,
    summary: str,
    condition: str,
    ref: str,
) -> list[WorkflowRunState]:
    if not names:
        return [run.model_copy(deep=True) for run in runs]
    targets = {name.strip().lower() for name in names}
    updated = [run.model_copy(deep=True) for run in runs]
    for run in updated:
        if run.name.strip().lower() not in targets:
            continue
        if run.status != WorkflowRunStatus.ACTIVE:
            continue
        run.status = WorkflowRunStatus.SATISFIED
        run.blocked_reason = ""
        run.evidence.append(
            WorkflowEvidence(
                kind=WorkflowStateEventKind.SATISFIED.value,
                ref=ref,
                ok=True,
                summary=summary,
                condition=condition,
            )
        )
    return updated


def _activated_successors(
    before: list[WorkflowRunState],
    after: list[WorkflowRunState],
) -> list[str]:
    before_active = {
        run.name
        for run in before
        if run.status == WorkflowRunStatus.ACTIVE
    }
    return [
        run.name
        for run in after
        if run.status == WorkflowRunStatus.ACTIVE and run.name not in before_active
    ]


def _node_transitioned_to_satisfied(runs: list[WorkflowRunState], name: str) -> bool:
    return any(
        run.name == name and run.status == WorkflowRunStatus.SATISFIED
        for run in runs
    )


def _next_hints(names: list[str]) -> list[str]:
    hints: list[str] = []
    service = WorkflowService()
    for name in names:
        node = service.get(name)
        if node is None:
            continue
        if node.goal:
            hints.append(node.goal)
        if node.rules:
            hints.append(node.rules[0])
        break
    return hints[:2]


def _first_hint(payload: dict) -> str:
    activated = payload.get("activated")
    active_name = ""
    if isinstance(activated, list) and activated:
        active_name = str(activated[0])
    if not active_name:
        return ""

    hints = payload.get("next_hints")
    first_hint = str(hints[0]) if isinstance(hints, list) and hints else ""
    parts = [f"Active workflow: {active_name}.", "Follow its gate before advancing again."]
    if first_hint:
        parts.append(first_hint)
    return " ".join(parts)


def _active_persona(runs: list[WorkflowRunState]) -> str | None:
    personas: list[str] = []
    for run in runs:
        if run.status != WorkflowRunStatus.ACTIVE:
            continue
        personas.extend(persona for persona in run.personas if persona)
    if not personas:
        return None
    return ",".join(dict.fromkeys(personas))
