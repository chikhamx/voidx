"""Workflow node lifecycle tool."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from voidx.runtime import GoalSpec, ToolStatePatch
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
    goal: str = Field(
        default="",
        max_length=120,
        description=(
            "Stable overall objective for the current task. Keep it short, sharp, and clear. "
            "Required for 'enter'. Optional retarget for 'advance'. Ignored for 'done'."
        ),
    )


class WorkflowTool(BaseTool):
    id = "workflow"
    description = (
        "Manage workflow node lifecycle. Enter a workflow before gated work, "
        "advance after its gate is satisfied, and use done only to close active nodes "
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
            return _enter(inp, runs, active, ctx)
        if inp.action == "advance":
            return _advance(inp, runs, active, ctx)
        return _done(inp, runs, active)


def _enter(inp: WorkflowInput, runs: list[WorkflowRunState], active: list[WorkflowRunState], ctx: ToolContext) -> ToolResult:
    requested = inp.workflow.strip()
    if not requested:
        return _guidance(
            action="enter",
            reason="node_required",
            guidance="Call workflow(action=\"enter\", workflow=\"<node>\", goal=\"<one-sentence goal>\") with a valid workflow node.",
            available_nodes=_available_nodes(),
            suggested_call='workflow(action="enter", workflow="debug", goal="...")',
        )

    node_name = _match_node(requested)
    if not node_name:
        return _guidance(
            action="enter",
            reason="invalid_node",
            guidance=f"Workflow node {requested!r} is not available. Choose one of the available nodes.",
            available_nodes=_available_nodes(),
            suggested_call='workflow(action="enter", workflow="debug", goal="...")',
        )

    goal = inp.goal.strip()
    if not goal:
        return _guidance(
            action="enter",
            reason="goal_required",
            guidance="Call workflow enter with a one-sentence goal for the workflow.",
            suggested_call=f'workflow(action="enter", workflow="{node_name}", goal="...")',
        )

    already_active = any(run.name == node_name and run.status == WorkflowRunStatus.ACTIVE for run in runs)
    if already_active and all(run.name == node_name for run in active):
        count = _track_repeat(ctx, _repeat_key("enter", node_name))
        updated = [run.model_copy(deep=True) for run in runs]
        for run in updated:
            if run.name == node_name and run.status == WorkflowRunStatus.ACTIVE:
                run.goal = goal
        payload = {
            "action": "enter",
            "workflow": node_name,
            "already_active": True,
            "activated": [node_name],
            "next_hints": _next_hints([node_name]),
            "goal": goal,
        }
        if count >= 2:
            guidance = _repeat_guidance(count, "enter", node_name)
            payload["repeat_warning"] = guidance
            if count >= _REPEAT_MAX:
                return ToolResult(
                    title=f"workflow: enter {node_name} (repeated {count}x)",
                    output=json.dumps(payload, ensure_ascii=False, indent=2),
                    summary=f"workflow enter {node_name} (repeated {count}x)",
                    metadata={"error": True, "reason": "repeated_workflow_enter", "guidance": guidance},
                    next_step_hint=guidance,
                )
        return _success(
            title=f"workflow: enter {node_name}",
            summary=f"workflow enter {node_name}",
            payload=payload,
            runs=updated,
            transition=payload,
            next_step_hint=_first_hint(payload),
            goal=goal,
        )

    updated = _satisfy_active_runs(
        runs,
        [run.name for run in active if run.name != node_name],
        summary=f"replaced by enter:{node_name}",
        condition=workflow_terminal_condition(),
        ref="tool:workflow",
    )
    updated = _activate_node(updated, node_name, goal=goal)
    payload = {
        "action": "enter",
        "workflow": node_name,
        "activated": [node_name],
        "next_hints": _next_hints([node_name]),
        "goal": goal,
    }
    return _success(
        title=f"workflow: enter {node_name}",
        summary=f"workflow enter {node_name}",
        payload=payload,
        runs=updated,
        transition=payload,
        next_step_hint=_first_hint(payload),
        goal=goal,
    )


def _advance(inp: WorkflowInput, runs: list[WorkflowRunState], active: list[WorkflowRunState], ctx: ToolContext) -> ToolResult:
    condition = inp.condition.strip()
    if not active:
        return _wrap_advance_guidance(ctx, _guidance(
            action="advance",
            reason="no_active_nodes",
            guidance="There is no active workflow node to advance.",
            available_exits=[],
            suggested_call='workflow(action="enter", workflow="debug", goal="...")',
        ), inp.workflow.strip() or condition)
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
        return _wrap_advance_guidance(ctx, guidance, inp.workflow.strip() or condition)
    assert selected is not None
    assert matched_condition is not None

    effective_goal, goal_source = _effective_goal(inp.goal.strip(), selected, ctx)
    if not effective_goal:
        return _wrap_advance_guidance(ctx, _guidance(
            action="advance",
            reason="goal_required",
            guidance="Advance requires a workflow goal from input, the active run, or current task goal.",
            available_exits=_available_exits(active),
            suggested_call=_suggested_advance_call(active),
        ), matched_condition)

    count = _track_repeat(ctx, _repeat_key("advance", selected.name, matched_condition))
    event = WorkflowStateEvent(
        workflow=selected.name,
        kind=WorkflowStateEventKind.SATISFIED,
        ref="tool:workflow",
        ok=True,
        summary=f"Workflow node {selected.name} completed.",
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
    updated = _apply_goal_to_runs(updated, activated, effective_goal)
    advanced = _node_transitioned_to_satisfied(updated, selected.name)
    if advanced:
        _reset_repeat(ctx, _repeat_key("advance", selected.name, matched_condition))
    payload = {
        "action": "advance",
        "from": selected.name,
        "condition": matched_condition,
        "activated": activated,
        "next_hints": _next_hints(activated),
        "goal": effective_goal,
        "goal_source": goal_source,
    }
    if count >= 2 and not advanced:
        guidance = _repeat_guidance(count, "advance", selected.name)
        payload["repeat_warning"] = guidance
        if count >= _REPEAT_MAX:
            return ToolResult(
                title=f"workflow: {selected.name} -> {matched_condition} (repeated {count}x)",
                output=json.dumps(payload, ensure_ascii=False, indent=2),
                summary=f"{selected.name} -> {matched_condition} (repeated {count}x)",
                metadata={"error": True, "reason": "repeated_workflow_advance", "guidance": guidance},
                next_step_hint=guidance,
            )
    return _success(
        title=f"workflow: {selected.name} -> {matched_condition}",
        summary=f"{selected.name} -> {matched_condition}",
        payload=payload,
        runs=updated,
        transition=payload,
        next_step_hint=_first_hint(payload),
        goal=effective_goal,
    )


def _done(inp: WorkflowInput, runs: list[WorkflowRunState], active: list[WorkflowRunState]) -> ToolResult:
    del inp
    if not active:
        payload = {
            "action": "done",
            "no_active_nodes": True,
            "activated": [],
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
        summary="Workflow node completed.",
        condition=workflow_terminal_condition(),
        ref="tool:workflow",
    )
    payload = {
        "action": "done",
        "from": names,
        "activated": [],
    }
    return _success(
        title="workflow: done",
        summary="workflow done",
        payload=payload,
        runs=updated,
        transition=payload,
    )


_REPEAT_MAX = 3
_STUCK_MAX = 3


def _repeat_key(action: str, node: str, condition: str = "") -> str:
    return f"{action}\x1f{node}\x1f{condition}"


def _track_repeat(ctx: ToolContext, key: str) -> int:
    tracker = ctx.workflow_repeat_tracker
    entry = tracker.get(key, {"count": 0})
    entry["count"] += 1
    tracker[key] = entry
    return entry["count"]


def _reset_repeat(ctx: ToolContext, key: str) -> None:
    ctx.workflow_repeat_tracker.pop(key, None)


def _wrap_advance_guidance(ctx: ToolContext, result: ToolResult, key_node: str) -> ToolResult:
    """Wrap an advance guidance result with repeat detection.

    Guidance-path repeats (advancing an already-satisfied node) use a separate
    counter from successful transitions, so a legitimate cross-task advance that
    resets the success counter does not mask a genuine stuck-loop on the guidance path.
    """
    count = _track_repeat(ctx, _repeat_key("advance_stuck", key_node))
    if count < 2:
        return result
    guidance = _repeat_guidance(count, "advance", key_node)
    payload = json.loads(result.output)
    payload["repeat_warning"] = guidance
    if count >= _STUCK_MAX:
        return ToolResult(
            title=result.title,
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            summary=result.summary,
            metadata={"error": True, "reason": "repeated_workflow_advance", "guidance": guidance},
            next_step_hint=guidance,
        )
    result.output = json.dumps(payload, ensure_ascii=False, indent=2)
    result.next_step_hint = guidance
    return result


def _repeat_guidance(count: int, action: str, node: str) -> str:
    if action == "advance":
        return _advance_repeat_guidance(count, node)
    return _enter_repeat_guidance(count, node)


def _advance_repeat_guidance(count: int, node: str) -> str:
    if count == 2:
        return (
            f"You already advanced {node!r} with this condition. "
            "The transition succeeded — do not call advance again. "
            "Proceed with the next node's workflow steps."
        )
    return (
        f"You have called advance {node!r} {count} times with the same condition. "
        "The transition already succeeded. Stop retrying — "
        "either proceed with the next node's workflow, or summarize the blocker and ask the user."
    )


def _enter_repeat_guidance(count: int, node: str) -> str:
    if count == 2:
        return (
            f"Node {node!r} is already active. You just called enter {node} again. "
            "Do not repeat this call — proceed with the node's workflow steps instead."
        )
    return (
        f"Node {node!r} is already active and you have called enter {node} {count} times. "
        "Stop retrying. Either advance the current node with a valid exit condition, "
        "or summarize the blocker and ask the user for input."
    )


def _success(
    *,
    title: str,
    summary: str,
    payload: dict,
    runs: list[WorkflowRunState],
    transition: dict,
    next_step_hint: str = "",
    goal: str | None = None,
) -> ToolResult:
    patch_args = {"workflow_runs": runs, "persona": _active_persona(runs)}
    include = {"workflow_runs", "persona"}
    if goal is not None:
        patch_args["goal"] = GoalSpec(desc=goal)
        include.add("goal")
    patch = ToolStatePatch(**patch_args)
    return ToolResult(
        title=title,
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        summary=summary,
        metadata={
            "workflow_transition": transition,
            "state_patch": patch.model_dump(mode="json", include=include),
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


def _effective_goal(input_goal: str, selected: WorkflowRunState, ctx: ToolContext) -> tuple[str, str]:
    if input_goal:
        return input_goal, "input"
    run_goal = selected.goal.strip()
    if run_goal:
        return run_goal, "run"
    current_goal = ctx.goal_target.strip()
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


def _activate_node(
    runs: list[WorkflowRunState],
    node_name: str,
    *,
    goal: str = "",
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


__all__ = ["WorkflowInput", "WorkflowTool"]
