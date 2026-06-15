"""Structured implementation plan approval tool."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from voidx.runtime import GoalSpec, GoalType, IntentResolution, TaskIntent, ToolStatePatch
from voidx.tools.base import BaseTool, ToolContext, ToolResult, UserInteraction, model_to_json_schema


class PlanStep(BaseModel):
    description: str = Field(description="What this implementation step does.")
    files: list[str] = Field(default_factory=list, description="Files touched by this step.")
    tool: str = Field(default="", description="Primary tool expected for this step.")


class PlanAlternative(BaseModel):
    name: str = Field(description="Short name for this alternative.")
    description: str = Field(description="What this approach would do differently.")
    trade_off: str = Field(default="", description="Why this alternative was not chosen.")


class PlanCheckpointInput(BaseModel):
    plan_summary: str = Field(description="Concise implementation plan summary.")
    steps: list[PlanStep] = Field(default_factory=list, description="Ordered implementation steps.")
    affected_files: list[str] = Field(default_factory=list, description="Files that may change.")
    risks: list[str] = Field(default_factory=list, description="Risks or trade-offs for the user.")
    alternatives: list[PlanAlternative] = Field(default_factory=list, description="Alternatives considered.")
    estimated_steps: int = Field(default=0, ge=0, description="Rough tool-call step estimate.")


class PlanCheckpointResult(BaseModel):
    plan_summary: str
    decision: str
    user_feedback: str = ""
    modified_scope: str = ""
    state_patch: ToolStatePatch | None = None


class PlanCheckpointTool(BaseTool):
    id = "plan_checkpoint"
    description = (
        "Present a concrete implementation plan for user approval before changing "
        "files, running write-capable commands, or delegating implementation. The "
        "user can approve, modify scope, or reject. This is a barrier tool: later "
        "tool calls in the same response are deferred until the decision updates "
        "runtime state."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(PlanCheckpointInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = PlanCheckpointInput.model_validate(args)
        if ctx.interact is None:
            return ToolResult(
                title="plan: approval unavailable",
                output=(
                    "Plan approval is not available in this runtime. "
                    f"Do not implement without explicit user approval: {inp.plan_summary}"
                ),
                metadata={"plan_decision": "interaction_unavailable", "blocked": True},
            )

        response = await ctx.interact(UserInteraction(
            prompt=_build_prompt(inp),
            options=[
                ("Approve", "approved", "Proceed with this plan"),
                ("Modify scope", "modified", "Approve with changes to scope"),
                ("Reject", "rejected", "Do not proceed"),
            ],
            blocking=True,
            timeout=120.0,
        ))
        if response.cancelled or response.value == "rejected":
            return _decision_result(inp, decision="rejected")
        if response.free_text:
            return _decision_result(inp, decision="modified", modified_scope=response.value.strip())
        if response.value == "modified":
            scope_response = await ctx.interact(UserInteraction(
                prompt="Describe the modified scope:",
                blocking=True,
                timeout=120.0,
            ))
            modified_scope = "" if scope_response.cancelled else scope_response.value.strip()
            return _decision_result(inp, decision="modified", modified_scope=modified_scope)
        if response.value == "approved":
            return _decision_result(inp, decision="approved")
        return _decision_result(inp, decision="modified", modified_scope=response.value.strip())


def _decision_result(
    inp: PlanCheckpointInput,
    *,
    decision: str,
    modified_scope: str = "",
) -> ToolResult:
    if decision == "approved":
        scope = inp.plan_summary.strip()
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING, desc=scope),
            goal=GoalSpec(type=GoalType.FEATURE, desc=scope),
        )
    elif decision == "modified":
        scope = modified_scope or inp.plan_summary.strip()
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING, desc=scope),
            goal=GoalSpec(type=GoalType.FEATURE, desc=scope),
        )
    else:
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING, desc=inp.plan_summary),
            goal=GoalSpec(type=GoalType.DESIGN, desc=inp.plan_summary),
        )

    result = PlanCheckpointResult(
        plan_summary=inp.plan_summary,
        decision=decision,
        user_feedback=modified_scope,
        modified_scope=modified_scope,
        state_patch=patch,
    )
    return ToolResult(
        title=f"plan: {decision}",
        output=json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        summary=f"plan {decision}",
        metadata={
            "plan_decision": decision,
            "state_patch": patch.model_dump(mode="json", exclude_unset=True),
        },
    )


def _build_prompt(inp: PlanCheckpointInput) -> str:
    parts = [f"Plan: {inp.plan_summary}"]
    if inp.steps:
        parts.append("\nSteps:")
        for index, step in enumerate(inp.steps, 1):
            files = f" ({', '.join(step.files)})" if step.files else ""
            parts.append(f"{index}. {step.description}{files}")
    if inp.affected_files:
        parts.append(f"\nAffected files: {', '.join(inp.affected_files)}")
    if inp.risks:
        parts.append("\nRisks:")
        parts.extend(f"- {risk}" for risk in inp.risks)
    if inp.alternatives:
        parts.append("\nAlternatives:")
        for alt in inp.alternatives:
            line = f"- {alt.name}: {alt.description}"
            if alt.trade_off:
                line = f"{line} ({alt.trade_off})"
            parts.append(line)
    if inp.estimated_steps:
        parts.append(f"\nEstimated steps: {inp.estimated_steps}")
    return "\n".join(parts)
