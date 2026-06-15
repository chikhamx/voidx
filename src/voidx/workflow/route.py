"""Workflow route boundary helpers."""

from __future__ import annotations

from voidx.workflow.policy import workflow_edges


def workflow_route_start(route: object) -> str:
    if route is None:
        return ""
    if isinstance(route, dict):
        return str(route.get("start") or "").strip().lower()
    return str(getattr(route, "start", "") or "").strip().lower()


def workflow_route_end(route: object) -> str:
    if route is None:
        return ""
    if isinstance(route, dict):
        return str(route.get("end") or "").strip().lower()
    return str(getattr(route, "end", "") or "").strip().lower()


def workflow_transition_target(workflow: str, condition: str) -> str:
    source = workflow.strip().lower()
    normalized_condition = condition.strip().lower()
    for edge in workflow_edges(source):
        if edge.condition == normalized_condition:
            return edge.target
    return ""


def workflow_path_reaches(start: str, target: str) -> bool:
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
        pending.extend(edge.target for edge in workflow_edges(current))
    return False
