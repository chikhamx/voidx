"""Goal lifecycle control tool for intake initialization and evaluator decisions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from voidx.runtime.goal import GoalSpec as AutonomousGoalSpec
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema


class GoalInput(BaseModel):
    op: Literal["init", "decision"] = Field(
        description="init submits the intake GoalSpec; decision submits the evaluator outcome."
    )
    objective: str = Field(
        default="",
        description="For op=init: required objective sentence. For op=decision: pass an empty string.",
    )
    acceptance_condition: str = Field(
        default="",
        description="For op=init: required verifiable done condition. For op=decision: pass an empty string.",
    )
    achievement_method: str = Field(
        default="",
        description="For op=init: optional execution guidance. For op=decision: pass an empty string.",
    )
    max_attempts: int = Field(
        default=20,
        ge=1,
        le=200,
        description="For op=init: attempt budget. For op=decision: pass 20.",
    )
    status: Literal["finished", "continue", "blocked", ""] = Field(
        default="",
        description="For op=decision: required outcome. For op=init: pass an empty string.",
    )
    summary: str = Field(
        default="",
        description="For op=decision: required summary. For op=init: pass an empty string.",
    )
    evidence: str = Field(default="", description="For op=decision: verification evidence; otherwise empty.")
    next: str = Field(default="", description="For op=decision: suggested next action; otherwise empty.")
    reason: str = Field(default="", description="For op=decision: stable reason/progress key; otherwise empty.")
    progress: Literal["none", "partial", "meaningful"] = Field(
        default="none",
        description="For op=decision: progress level; for op=init pass none.",
    )

    @field_validator("objective", "acceptance_condition", "achievement_method", "summary", "evidence", "next", "reason")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class GoalTool(BaseTool):
    id = "goal"
    description = (
        "Initialize or decide a runtime-backed Goal. During intake, call op='init' "
        "with objective and acceptance_condition; set decision-only fields to empty strings. "
        "During evaluator, call op='decision' with status and summary; set init-only text fields "
        "to empty strings. Calls without op are invalid."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(GoalInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = GoalInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        if inp.op == "init":
            return await _submit_init(inp, ctx)
        return await _submit_decision(inp, ctx)


async def _submit_init(inp: GoalInput, ctx: ToolContext) -> ToolResult:
    controller = ctx.goal_intake_controller
    if ctx.goal_phase != "intake" or controller is None:
        return ToolResult(
            output="Goal init is intake-only; this call was not submitted.",
            metadata={"goal_init_submitted": False, "guidance_only": True},
        )
    try:
        spec = AutonomousGoalSpec(
            objective=inp.objective,
            acceptance_condition=inp.acceptance_condition,
            achievement_method=inp.achievement_method,
            max_attempts=inp.max_attempts,
        )
    except Exception as exc:
        return ToolResult(output=f"Invalid goal init: {exc}", metadata={"error": True})
    submitted = await controller.submit_init(spec)
    return ToolResult(
        output="Goal init recorded.",
        metadata={
            "goal_init_submitted": True,
            "goal_spec": submitted.model_dump(mode="json"),
        },
    )


async def _submit_decision(inp: GoalInput, ctx: ToolContext) -> ToolResult:
    if not inp.status:
        return ToolResult(output="Invalid goal decision: status is required.", metadata={"error": True})
    if not inp.summary:
        return ToolResult(output="Invalid goal decision: summary is required.", metadata={"error": True})
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
