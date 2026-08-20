"""Workflow route boundary helpers."""

from __future__ import annotations

from voidx.agent.domain.automation.workflow_policy import workflow_edges
from voidx.agent.domain.automation.workflow_schema import WorkflowDAG


def workflow_route_start(route: object) -> str:
    if route is None:
        return ""
    if isinstance(route, dict):
        return str(route.get("join") or route.get("start", "")).strip().lower()
    return str(getattr(route, "join", "") or getattr(route, "start", "")).strip().lower()


def workflow_route_end(route: object) -> str:
    if route is None:
        return ""
    if isinstance(route, dict):
        return str(route.get("leave") or route.get("end", "")).strip().lower()
    return str(getattr(route, "leave", "") or getattr(route, "end", "")).strip().lower()


def workflow_transition_target(workflow: str, condition: str, dag: WorkflowDAG) -> str:
    source = workflow.strip().lower()
    normalized_condition = condition.strip().lower()
    for edge in workflow_edges(source, dag):
        if edge.condition == normalized_condition:
            return edge.target
    return ""


def workflow_path_reaches(start: str, target: str, dag: WorkflowDAG) -> bool:
    normalized_start = start.strip().lower()
    normalized_target = target.strip().lower()
    if not normalized_start or not normalized_target:
        return False

    seen: set[str] = set()
    pending = [normalized_start]
    while pending:
        current = pending.pop()
        if current == normalized_target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(edge.target for edge in workflow_edges(current, dag))
    return False
