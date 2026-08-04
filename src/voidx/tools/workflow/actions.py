from __future__ import annotations
import json

from typing import TYPE_CHECKING

from voidx.tools.base import ToolContext, ToolResult
from voidx.workflow.service import advance_workflow_states, workflow_terminal_condition
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus, WorkflowStateEvent, WorkflowStateEventKind
from .queries import (
    _available_exits, _available_nodes, _invalid_exit_guidance, _match_condition,
    _match_node, _select_advance_run,
    _suggested_advance_call,
)
from .result import _guidance, _success
from .state import (
    _activated_successors, _active_runs, _activate_node, _apply_goal_to_runs,
    _current_runs, _effective_goal, _first_hint, _node_transitioned_to_satisfied,
    _next_hints, _satisfy_active_runs,
)
from voidx.tools.workflow_guidance import (
    REPEAT_MAX as _REPEAT_MAX, repeat_guidance as _repeat_guidance,
    repeat_key as _repeat_key, reset_repeat as _reset_repeat, track_repeat as _track_repeat,
    wrap_advance_guidance as _wrap_advance_guidance,
)

if TYPE_CHECKING:
    from . import WorkflowInput


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

    normalized_name = node_name.strip().lower()
    already_active = any(
        run.name.strip().lower() == normalized_name and run.status == WorkflowRunStatus.ACTIVE
        for run in runs
    )
    if already_active and all(run.name.strip().lower() == normalized_name for run in active):
        count = _track_repeat(ctx, _repeat_key("enter", node_name))
        updated = [run.model_copy(deep=True) for run in runs]
        for run in updated:
            if run.name.strip().lower() == normalized_name and run.status == WorkflowRunStatus.ACTIVE:
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
