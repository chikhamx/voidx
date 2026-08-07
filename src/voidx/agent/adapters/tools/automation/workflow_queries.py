from __future__ import annotations

from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.agent.application.automation.workflow.service import (
    WorkflowService, workflow_edges, workflow_terminal_condition,
    workflow_terminal_description,
)
from voidx.agent.domain.automation.workflow import WorkflowRunState
from .workflow_result import _guidance

def _select_advance_run(
    active: list[WorkflowRunState],
    condition: str,
    *,
    workflow: str = "",
) -> tuple[WorkflowRunState | None, str | None, ToolResult | None]:
    requested = workflow.strip()
    if requested:
        selected = _match_active_run(active, requested)
        if selected is None:
            current_node = active[0].name if active else ""
            return None, None, _guidance(
                action="advance",
                reason="invalid_active_workflow",
                guidance=(
                    f"Workflow node {requested!r} is not currently active. "
                    f"Current node: {current_node}. "
                    "Omit the workflow parameter to use the current node."
                ),
                current_node=current_node,
                suggested_call=_suggested_advance_call(active),
            )
        matched = _match_condition(condition, selected.name)
        if matched is None:
            return None, None, _invalid_exit_guidance(condition, [selected])
        return selected, matched, None

    candidates: list[tuple[WorkflowRunState, str]] = []
    for run in active:
        matched = _match_condition(condition, run.name)
        if matched is not None:
            candidates.append((run, matched))
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1], None
    if len(candidates) > 1:
        names = [run.name for run, _ in candidates]
        return None, None, _guidance(
            action="advance",
            reason="ambiguous_exit",
            guidance=(
                f"Condition {condition!r} matches multiple active workflow nodes. "
                "Pass the workflow node name explicitly."
            ),
            candidates=names,
            available_exits=_available_exits(active),
            suggested_call=(
                f'workflow(action="advance", workflow="{names[0]}", '
                f'condition="{candidates[0][1]}")'
            ),
        )
    return None, None, _invalid_exit_guidance(condition, active)


def _invalid_exit_guidance(condition: str, active: list[WorkflowRunState]) -> ToolResult:
    return _guidance(
        action="advance",
        reason="invalid_exit",
        guidance=f"Condition {condition!r} does not match an available workflow exit.",
        available_exits=_available_exits(active),
        suggested_call=_suggested_advance_call(active),
    )


def _match_active_run(active: list[WorkflowRunState], name: str) -> WorkflowRunState | None:
    normalized = name.strip().lower()
    for run in active:
        if run.name.strip().lower() == normalized:
            return run
    return None


def _match_node(name: str) -> str | None:
    normalized = name.strip().lower()
    for node in WorkflowService().nodes():
        if node.name.strip().lower() == normalized:
            return node.name
    return None


def _match_condition(condition: str, workflow: str) -> str | None:
    normalized = condition.strip().lower()
    if normalized == workflow_terminal_condition():
        return None
    for edge in workflow_edges(workflow):
        if edge.condition.strip().lower() == normalized:
            return edge.condition
    return None


def _available_nodes() -> list[str]:
    return [node.name for node in WorkflowService().nodes()]


def _available_exits(active: list[WorkflowRunState]) -> list[str]:
    lines: list[str] = []
    for run in active:
        for edge in workflow_edges(run.name):
            suffix = f" ({edge.label})" if edge.label else ""
            lines.append(f"{edge.condition} -> {edge.target}{suffix}")
    if active:
        lines.append(f"{workflow_terminal_condition()} -> {workflow_terminal_description()}")
    return lines


def _suggested_advance_call(active: list[WorkflowRunState]) -> str:
    for run in active:
        edges = workflow_edges(run.name)
        if edges:
            edge = edges[0]
            return f'workflow(action="advance", condition="{edge.condition}")'
    return 'workflow(action="done")'


