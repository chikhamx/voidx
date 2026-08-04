from __future__ import annotations

import json

from voidx.runtime import GoalSpec, ToolStatePatch
from voidx.tools.base import ToolContext, ToolResult
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus
from .state import _active_persona

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

