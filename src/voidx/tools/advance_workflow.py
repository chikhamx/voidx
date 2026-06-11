"""Workflow node transition tool."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from voidx.runtime import ToolStatePatch
from voidx.workflow.policy import (
    is_workflow_terminal_condition,
    workflow_edges,
    workflow_sort_key,
    workflow_terminal_condition,
    workflow_terminal_description,
)
from voidx.workflow.runtime import (
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
    advance_workflow_states,
)
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema


class AdvanceWorkflowInput(BaseModel):
    workflow: str = Field(
        default="",
        description=(
            "Optional active workflow node name to advance. Required when using "
            f"'{workflow_terminal_condition()}' and multiple workflow nodes are active."
        ),
    )
    condition: str = Field(
        default=workflow_terminal_condition(),
        description=(
            "Transition condition to take from the current workflow node. "
            "Must match an outgoing edge condition in the workflow DAG. "
            f"Use '{workflow_terminal_condition()}' to {workflow_terminal_description()}."
        ),
    )
    evidence: str = Field(default="", description="Brief evidence that the condition is satisfied.")
    summary: str = Field(default="", description="What was accomplished in the current workflow node.")


class AdvanceWorkflowTool(BaseTool):
    id = "advance_workflow"
    description = (
        "Choose the exit condition for the current workflow node. Use this "
        "when a workflow node is complete before proceeding to its next phase. "
        "The condition must be one of the active node's available workflow exits, "
        f"or '{workflow_terminal_condition()}' to {workflow_terminal_description()}."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(AdvanceWorkflowInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = AdvanceWorkflowInput.model_validate(args)
        workflow = inp.workflow.strip().lower()
        condition = inp.condition.strip() or workflow_terminal_condition()
        runs = _current_runs(ctx)
        active = _active_runs(runs)
        if not active:
            return ToolResult(
                title="workflow: no active node",
                output="No active workflow node is available to advance.",
                metadata={"error": True},
            )

        if not workflow and is_workflow_terminal_condition(condition) and len(active) > 1:
            return ToolResult(
                title="workflow: ambiguous target",
                output=_ambiguous_target_message(active),
                metadata={"error": True, "ambiguous": True, "condition": condition},
            )

        selected = _select_run(active, condition, workflow=workflow)
        if selected is None:
            return ToolResult(
                title="workflow: invalid exit",
                output=_invalid_condition_message(condition, active, workflow=workflow),
                metadata={"error": True, "condition": condition, "workflow": workflow},
            )

        event = WorkflowStateEvent(
            workflow=selected.name,
            kind=WorkflowStateEventKind.SATISFIED,
            ref="tool:advance_workflow",
            ok=True,
            summary=inp.summary or f"Workflow node {selected.name} completed.",
            reason=inp.evidence,
            condition=condition,
        )
        updated = advance_workflow_states(runs, [event])
        patch = ToolStatePatch(skill_runs=updated)
        payload = {
            "from": selected.name,
            "condition": condition,
            "activated": _activated_successors(runs, updated),
            "summary": event.summary,
            "evidence": inp.evidence,
        }
        return ToolResult(
            title=f"workflow: {selected.name} -> {condition}",
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            metadata={
                "workflow_transition": payload,
                "state_patch": patch.model_dump(mode="json", include={"skill_runs"}),
            },
        )


def _current_runs(ctx: ToolContext) -> list[WorkflowRunState]:
    runs = [item.model_copy(deep=True) for item in ctx.skill_runs]
    if runs:
        return runs
    return [
        WorkflowRunState(name=name, status=WorkflowRunStatus.ACTIVE)
        for name in ctx.active_skill_names
        if name.strip()
    ]


def _active_runs(runs: list[WorkflowRunState]) -> list[WorkflowRunState]:
    active = [run for run in runs if run.status == WorkflowRunStatus.ACTIVE]
    return sorted(active, key=lambda run: workflow_sort_key(run.name))


def _select_run(
    active: list[WorkflowRunState],
    condition: str,
    *,
    workflow: str = "",
) -> WorkflowRunState | None:
    if workflow:
        selected = next((run for run in active if run.name == workflow), None)
        if selected is None:
            return None
        if is_workflow_terminal_condition(condition):
            return selected
        if any(edge.condition == condition for edge in workflow_edges(selected.name)):
            return selected
        return None
    if is_workflow_terminal_condition(condition):
        return active[0]
    for run in active:
        if any(edge.condition == condition for edge in workflow_edges(run.name)):
            return run
    return None


def _invalid_condition_message(
    condition: str,
    active: list[WorkflowRunState],
    *,
    workflow: str = "",
) -> str:
    if workflow and not any(run.name == workflow for run in active):
        active_names = ", ".join(run.name for run in active) or "none"
        return f"Invalid workflow target: {workflow!r}. Active workflow nodes: {active_names}."
    lines = [f"Invalid workflow condition: {condition!r}. Available exits:"]
    found = False
    for run in active:
        if workflow and run.name != workflow:
            continue
        edges = workflow_edges(run.name)
        if not edges:
            continue
        found = True
        lines.append(f"- {run.name}:")
        for edge in edges:
            label = f" ({edge.label})" if edge.label else ""
            lines.append(f"  - {edge.condition} -> {edge.target}{label}")
    if not found:
        lines.append("- No outgoing edges are available.")
    lines.append(f"- {workflow_terminal_condition()} -> {workflow_terminal_description()}")
    return "\n".join(lines)


def _ambiguous_target_message(active: list[WorkflowRunState]) -> str:
    lines = [
        "Ambiguous workflow target for terminal condition 'done'.",
        "Pass the workflow node name explicitly, for example:",
        "advance_workflow(workflow=\"writing-design-docs\", condition=\"done\")",
        "Active workflow nodes:",
    ]
    lines.extend(f"- {run.name}" for run in active)
    return "\n".join(lines)


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
