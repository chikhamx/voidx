"""Shared workflow run helpers for the agent graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voidx.workflow.types import WorkflowRunState


def active_workflow_names(value: object) -> list[str]:
    """Extract names of active workflow runs from various input shapes.

    Accepts a list/dict of WorkflowRunState or raw dicts, or an object
    with a ``workflow_runs`` attribute.
    """
    from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus

    items: list[WorkflowRunState] = []
    raw = value

    if hasattr(value, "workflow_runs"):
        raw = getattr(value, "workflow_runs", None) or {}

    iterable = raw.values() if isinstance(raw, dict) else raw or []
    for item in iterable:
        try:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        except (TypeError, ValueError):
            continue
        items.append(run)

    return [run.name.strip() for run in items if run.status == WorkflowRunStatus.ACTIVE and run.name.strip()]
