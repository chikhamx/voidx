"""Workflow node lifecycle tool."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.arguments import (
    drop_nullish_tool_fields,
    keep_tool_args,
)
from voidx.tooling.domain.schema import model_to_json_schema
from .workflow_actions import _advance, _done, _enter
from .workflow_result import _guidance
from voidx.agent.adapters.tools.automation.workflow_state import _active_runs, _current_runs


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


def _normalize_workflow_args(args):
    if not isinstance(args, dict):
        return args
    action = str(args.get("action") or "").strip().lower()
    if action == "enter":
        return drop_nullish_tool_fields(
            keep_tool_args(args, {"action", "workflow", "goal"}), "workflow", "goal"
        )
    if action == "advance":
        return drop_nullish_tool_fields(
            keep_tool_args(args, {"action", "workflow", "condition", "goal"}),
            "workflow",
            "condition",
            "goal",
        )
    if action == "done":
        return drop_nullish_tool_fields(
            keep_tool_args(args, {"action", "workflow"}), "workflow"
        )
    return args


class WorkflowTool:
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
        raw_args = _normalize_workflow_args(raw_args)
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



__all__ = ["WorkflowInput", "WorkflowTool"]
