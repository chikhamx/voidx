"""Goal lifecycle control tool; executable only during evaluator phase."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema


class GoalDecisionInput(BaseModel):
    status: Literal["finished", "continue", "blocked"]
    summary: str = Field(default="")
    evidence: str = Field(default="")
    next: str = Field(default="")
    reason: str = Field(default="")
    progress: Literal["none", "partial", "meaningful"] = "none"


class GoalTool(BaseTool):
    id = "goal"
    description = (
        "Submit the Goal lifecycle decision after evaluating the acceptance condition. "
        "Use verification tools first when necessary."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(GoalDecisionInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = GoalDecisionInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        controller = ctx.goal_controller
        if ctx.goal_phase != "evaluator" or controller is None:
            return ToolResult(
                output="Goal decisions are evaluator-only; this call was not submitted.",
                metadata={"goal_decision_submitted": False, "guidance_only": True},
            )
        outcome = {"finished": "completed", "continue": "continue", "blocked": "blocked"}[inp.status]
        decision = await controller.submit_decision(
            {
                "outcome": outcome,
                "summary": inp.summary,
                "evidence": inp.evidence,
                "next": inp.next,
                "reason": inp.reason,
                "progress": inp.progress,
            }
        )
        return ToolResult(
            output=f"Goal decision recorded: {inp.status}.",
            metadata={"goal_decision_submitted": True, "outcome": decision.outcome},
        )
