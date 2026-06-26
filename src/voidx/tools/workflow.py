"""Workflow node lifecycle tool."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from voidx.runtime import ToolStatePatch
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema
from voidx.workflow.service import (
    WorkflowService,
    advance_workflow_states,
    workflow_edges,
    workflow_sort_key,
    workflow_terminal_condition,
    workflow_terminal_description,
    workflow_transitions,
)
from voidx.workflow.types import (
    WorkflowActivationSource,
    WorkflowEvidence,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
)


_WORKFLOW_ACTIONS = ("enter", "advance", "done")


class WorkflowInput(BaseModel):
    action: Literal["enter", "advance", "done"] = Field(description="Workflow operation.")
    workflow: str = Field(
        default="",
        description=(
            "Workflow node name. Required for 'enter'. For 'advance'/'done', "
            "auto-resolved when only one active node exists."
        ),
    )
    condition: str = Field(
        default="",
        description=(
            "Exit condition for 'advance'. Must match an outgoing edge condition "
            "in the workflow DAG (case-insensitive). Ignored for 'enter' and 'done'."
        ),
    )
    evidence: str = Field(
        default="",
        description="Brief evidence that the condition is satisfied. Required for 'advance' and 'done'.",
    )


class WorkflowTool(BaseTool):
    id = "workflow"
    description = (
        "Manage workflow node lifecycle. Use 'enter' to activate a workflow node, "
        "'advance' to transition via an exit condition, or 'done' to end a node "
        "without activating successors."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(WorkflowInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        raw_args = dict(args or {})
        action = str(raw_args.get("action") or "").strip()
        if not action:
            return _guidance(
                action="",
                reason="action_required",
                guidance="Call workflow with action set to one of: enter, advance, done.",
                available_actions=list(_WORKFLOW_ACTIONS),
                suggested_call='workflow(action="enter", workflow="debug")',
            )
        canonical_action = action.lower()
        if canonical_action not in _WORKFLOW_ACTIONS:
            return _guidance(
                action=action,
                reason="invalid_action",
                guidance="Workflow action must be one of: enter, advance, done.",
                available_actions=list(_WORKFLOW_ACTIONS),
                suggested_call='workflow(action="enter", workflow="debug")',
            )
        raw_args["action"] = canonical_action
        try:
            inp = WorkflowInput.model_validate(raw_args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        runs = _current_runs(ctx)
        active = _active_runs(runs)

        if inp.action == "enter":
            return _enter(inp, runs, active)
        if inp.action == "advance":
            return _advance(inp, runs, active)
        return _done(inp, runs, active)


def _enter(inp: WorkflowInput, runs: list[WorkflowRunState], active: list[WorkflowRunState]) -> ToolResult:
    requested = inp.workflow.strip()
    if not requested:
        return _guidance(
            action="enter",
            reason="node_required",
            guidance="Call workflow(action=\"enter\", workflow=\"<node>\") with a valid workflow node.",
            available_nodes=_available_nodes(),
            suggested_call='workflow(action="enter", workflow="debug")',
        )

    node_name = _match_node(requested)
    if not node_name:
        return _guidance(
            action="enter",
            reason="invalid_node",
            guidance=f"Workflow node {requested!r} is not available. Choose one of the available nodes.",
            available_nodes=_available_nodes(),
            suggested_call='workflow(action="enter", workflow="debug")',
        )

    already_active = any(run.name == node_name and run.status == WorkflowRunStatus.ACTIVE for run in runs)
    if already_active and all(run.name == node_name for run in active):
        updated = [run.model_copy(deep=True) for run in runs]
        patch = ToolStatePatch(workflow_runs=updated, persona=_active_persona(updated))
        payload = {
            "action": "enter",
            "workflow": node_name,
            "already_active": True,
            "activated": [node_name],
            "next_hints": _next_hints([node_name]),
            "evidence": inp.evidence.strip(),
        }
        return _success(
            title=f"workflow: enter {node_name}",
            summary=f"workflow enter {node_name}",
            payload=payload,
            runs=updated,
            transition=payload,
            next_step_hint=_first_hint(payload),
        )

    updated = _satisfy_active_runs(
        runs,
        [run.name for run in active if run.name != node_name],
        summary=f"replaced by enter:{node_name}",
        condition=workflow_terminal_condition(),
        ref="tool:workflow",
    )
    updated = _activate_node(updated, node_name, evidence=inp.evidence.strip())
    patch = ToolStatePatch(workflow_runs=updated, persona=_active_persona(updated))
    payload = {
        "action": "enter",
        "workflow": node_name,
        "activated": [node_name],
        "next_hints": _next_hints([node_name]),
        "evidence": inp.evidence.strip(),
    }
    return _success(
        title=f"workflow: enter {node_name}",
        summary=f"workflow enter {node_name}",
        payload=payload,
        runs=patch.workflow_runs,
        transition=payload,
        next_step_hint=_first_hint(payload),
    )


def _advance(inp: WorkflowInput, runs: list[WorkflowRunState], active: list[WorkflowRunState]) -> ToolResult:
    condition = inp.condition.strip()
    if not active:
        return _guidance(
            action="advance",
            reason="no_active_nodes",
            guidance="There is no active workflow node to advance.",
            available_exits=[],
            suggested_call='workflow(action="enter", workflow="debug")',
        )
    if not condition:
        return _guidance(
            action="advance",
            reason="condition_required",
            guidance="Call workflow with an exit condition from the active node.",
            available_exits=_available_exits(active),
            suggested_call=_suggested_advance_call(active),
        )

    selected, matched_condition, guidance = _select_advance_run(active, condition, workflow=inp.workflow)
    if guidance is not None:
        return guidance
    assert selected is not None
    assert matched_condition is not None

    evidence = inp.evidence.strip()
    event = WorkflowStateEvent(
        workflow=selected.name,
        kind=WorkflowStateEventKind.SATISFIED,
        ref="tool:workflow",
        ok=True,
        summary=f"Workflow node {selected.name} completed.",
        reason=evidence,
        condition=matched_condition,
    )
    updated = advance_workflow_states(runs, [event])
    updated = _satisfy_active_runs(
        updated,
        [run.name for run in active if run.name != selected.name],
        summary=f"Superseded by workflow advance from {selected.name}.",
        condition="superseded_by_workflow_advance",
        ref="tool:workflow",
    )
    activated = _activated_successors(runs, updated)
    patch = ToolStatePatch(workflow_runs=updated, persona=_active_persona(updated))
    payload = {
        "action": "advance",
        "from": selected.name,
        "condition": matched_condition,
        "activated": activated,
        "next_hints": _next_hints(activated),
        "evidence": evidence,
    }
    return _success(
        title=f"workflow: {selected.name} -> {matched_condition}",
        summary=f"{selected.name} -> {matched_condition}",
        payload=payload,
        runs=patch.workflow_runs,
        transition=payload,
        next_step_hint=_first_hint(payload),
    )


def _done(inp: WorkflowInput, runs: list[WorkflowRunState], active: list[WorkflowRunState]) -> ToolResult:
    evidence = inp.evidence.strip()
    if not active:
        payload = {
            "action": "done",
            "no_active_nodes": True,
            "activated": [],
            "evidence": evidence,
        }
        return _success(
            title="workflow: done",
            summary="workflow done",
            payload=payload,
            runs=runs,
            transition=payload,
        )

    names = [run.name for run in active]
    updated = _satisfy_active_runs(
        runs,
        names,
        summary=evidence or "Workflow node completed.",
        condition=workflow_terminal_condition(),
        ref="tool:workflow",
    )
    patch = ToolStatePatch(workflow_runs=updated, persona=_active_persona(updated))
    payload = {
        "action": "done",
        "from": names,
        "activated": [],
        "evidence": evidence,
    }
    return _success(
        title="workflow: done",
        summary="workflow done",
        payload=payload,
        runs=patch.workflow_runs,
        transition=payload,
    )


def _success(
    *,
    title: str,
    summary: str,
    payload: dict,
    runs: list[WorkflowRunState],
    transition: dict,
    next_step_hint: str = "",
) -> ToolResult:
    patch = ToolStatePatch(workflow_runs=runs, persona=_active_persona(runs))
    return ToolResult(
        title=title,
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        summary=summary,
        metadata={
            "workflow_transition": transition,
            "state_patch": patch.model_dump(mode="json", include={"workflow_runs", "persona"}),
        },
        next_step_hint=next_step_hint,
    )


def _guidance(action: str, reason: str, guidance: str, **extra: object) -> ToolResult:
    payload = {
        "action": action,
        "applied": False,
        "reason": reason,
        "guidance": guidance,
        **extra,
    }
    return ToolResult(
        title=f"workflow: {reason}",
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        summary=guidance,
        metadata={"workflow_guidance": payload},
    )


def _current_runs(ctx: ToolContext) -> list[WorkflowRunState]:
    runs_by_name: dict[str, WorkflowRunState] = {}
    for item in ctx.workflow_runs:
        run = item.model_copy(deep=True)
        key = run.name.strip().lower()
        if not key:
            continue
        runs_by_name[key] = run
    runs = list(runs_by_name.values())
    if runs:
        return runs
    return [
        WorkflowRunState(name=name.strip().lower(), status=WorkflowRunStatus.ACTIVE)
        for name in ctx.active_workflow_names
        if name.strip()
    ]


def _active_runs(runs: list[WorkflowRunState]) -> list[WorkflowRunState]:
    active = [run for run in runs if run.status == WorkflowRunStatus.ACTIVE]
    return sorted(active, key=lambda run: workflow_sort_key(run.name))


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
            return None, None, _guidance(
                action="advance",
                reason="invalid_active_workflow",
                guidance=f"Workflow node {requested!r} is not currently active.",
                active_nodes=[run.name for run in active],
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
                f'condition="{candidates[0][1]}", evidence="...")'
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
            return f'workflow(action="advance", condition="{edge.condition}", evidence="...")'
    return 'workflow(action="done", evidence="...")'


def _activate_node(
    runs: list[WorkflowRunState],
    node_name: str,
    *,
    evidence: str = "",
) -> list[WorkflowRunState]:
    updated = [run.model_copy(deep=True) for run in runs]
    node = WorkflowService().get(node_name)
    personas = [node.persona] if node is not None and node.persona else []
    existing = next((run for run in updated if run.name == node_name), None)
    if existing is None:
        existing = WorkflowRunState(name=node_name)
        updated.append(existing)
    existing.status = WorkflowRunStatus.ACTIVE
    existing.source = WorkflowActivationSource.MANUAL
    existing.reason = "manual:enter"
    existing.goal_type = ""
    existing.scope = ""
    existing.personas = personas
    existing.blocked_reason = ""
    existing.transition_to = list(workflow_transitions(node_name))
    existing.evidence.append(
        WorkflowEvidence(
            kind=WorkflowStateEventKind.ACTIVATED.value,
            ref="tool:workflow",
            ok=True,
            summary=evidence or "Manual workflow activation.",
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
    hints = payload.get("next_hints")
    if isinstance(hints, list) and hints:
        return str(hints[0])
    return ""


def _active_persona(runs: list[WorkflowRunState]) -> str | None:
    personas: list[str] = []
    for run in runs:
        if run.status != WorkflowRunStatus.ACTIVE:
            continue
        personas.extend(persona for persona in run.personas if persona)
    if not personas:
        return None
    return ",".join(dict.fromkeys(personas))


__all__ = ["WorkflowInput", "WorkflowTool"]
