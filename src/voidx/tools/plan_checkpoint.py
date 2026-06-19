"""Structured implementation plan approval tool."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from voidx.runtime import GoalSpec, GoalType, IntentResolution, PlanResolution, TaskIntent, ToolStatePatch
from voidx.tools.base import BaseTool, ToolContext, ToolResult, UserInteraction, model_to_json_schema
from voidx.workflow.policy import workflow_transitions
from voidx.workflow.service import WorkflowService
from voidx.workflow.types import (
    WorkflowActivationSource,
    WorkflowEvidence,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEventKind,
)


class PlanCheckpointInput(BaseModel):
    plan_summary: str = Field(description="Concise implementation plan summary.")
    steps: list[str] = Field(default_factory=list, description="Ordered implementation steps.")
    affected_files: list[str] = Field(
        default_factory=list,
        description="All files that may be created or modified across all steps.",
    )
    risks: list[str] = Field(default_factory=list, description="Risks, edge cases, or trade-offs to consider.")


class PlanCheckpointResult(BaseModel):
    plan_summary: str
    decision: str
    user_feedback: str = ""
    modified_scope: str = ""
    state_patch: ToolStatePatch | None = None


_CHECKPOINT_OPTIONS: list[tuple[str, str, str]] = [
    ("Implement directly", "approved", "Start implementing the plan"),
    ("Document first", "needs_doc", "Write a design document before implementing"),
    ("Modify scope", "modified", "Adjust the plan scope"),
    ("Reject", "rejected", "Do not proceed"),
]

_DECISION_MAP: dict[str, str] = {
    "approved": "approved",
    "needs_doc": "needs_doc",
    "modified": "modified",
    "rejected": "rejected",
}


class PlanCheckpointTool(BaseTool):
    id = "checkpoint"
    description = (
        "Present a concrete implementation plan for user approval before changing "
        "files, running write-capable commands, or delegating implementation. The "
        "user can approve, request a design document first, modify scope, or "
        "reject. Later tool calls in the same response are deferred until the "
        "decision updates runtime state."
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
            options=_CHECKPOINT_OPTIONS,
            timeout=120.0,
        ))
        decision = _DECISION_MAP.get(response.value, "modified")
        if response.cancelled or decision == "rejected":
            return _decision_result(inp, decision="rejected")
        if response.free_text:
            return _decision_result(inp, decision="modified", modified_scope=response.value.strip())
        if decision == "modified":
            scope_response = await ctx.interact(UserInteraction(
                prompt="Describe the modified scope:",
                timeout=120.0,
            ))
            modified_scope = "" if scope_response.cancelled else scope_response.value.strip()
            return _decision_result(inp, decision="modified", modified_scope=modified_scope)
        if decision == "approved":
            return _decision_result(
                inp,
                decision="approved",
                workflow_runs=ctx.workflow_runs,
                turn_count=ctx.turn_count,
            )
        if decision == "needs_doc":
            return _decision_result(
                inp,
                decision="needs_doc",
                workflow_runs=ctx.workflow_runs,
                turn_count=ctx.turn_count,
            )
        return _decision_result(inp, decision="modified", modified_scope=response.value.strip())


def _decision_result(
    inp: PlanCheckpointInput,
    *,
    decision: str,
    modified_scope: str = "",
    workflow_runs: list[WorkflowRunState] | None = None,
    turn_count: int = 0,
) -> ToolResult:
    if decision == "approved":
        scope = inp.plan_summary.strip()
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING, desc=scope),
            goal=GoalSpec(type=GoalType.FEATURE, desc=scope),
            plan=PlanResolution(join="tdd", leave="verify"),
            workflow_runs=_checkpoint_workflow_runs(
                workflow_runs or (),
                target="tdd",
                scope=scope,
                decision=decision,
                turn_count=turn_count,
            ),
        )
        next_step_hint = ""
    elif decision == "needs_doc":
        scope = inp.plan_summary.strip()
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING, desc=scope),
            goal=GoalSpec(type=GoalType.DOC, desc=scope),
            plan=PlanResolution(join="design", leave="design"),
            workflow_runs=_checkpoint_workflow_runs(
                workflow_runs or (),
                target="design",
                scope=scope,
                decision=decision,
                turn_count=turn_count,
            ),
        )
        next_step_hint = ""
    elif decision == "modified":
        scope = modified_scope or inp.plan_summary.strip()
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING, desc=scope),
            goal=GoalSpec(type=GoalType.FEATURE, desc=scope),
        )
        next_step_hint = ""
    else:
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING, desc=inp.plan_summary),
            goal=GoalSpec(type=GoalType.FEATURE, desc=inp.plan_summary),
        )
        next_step_hint = ""

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
        next_step_hint=next_step_hint,
    )


def _build_prompt(inp: PlanCheckpointInput) -> str:
    parts = [f"Plan: {inp.plan_summary}"]
    if inp.steps:
        parts.append("\nSteps:")
        for index, step in enumerate(inp.steps, 1):
            parts.append(f"{index}. {step}")
    if inp.affected_files:
        parts.append(f"\nAffected files: {', '.join(inp.affected_files)}")
    if inp.risks:
        parts.append("\nRisks:")
        parts.extend(f"- {risk}" for risk in inp.risks)
    return "\n".join(parts)


def _checkpoint_workflow_runs(
    current: list[WorkflowRunState] | tuple[WorkflowRunState, ...],
    *,
    target: str,
    scope: str,
    decision: str,
    turn_count: int = 0,
) -> list[WorkflowRunState]:
    target_name = target.strip().lower()
    service = WorkflowService()
    node = service.get(target_name)
    if node is None:
        return [run.model_copy(deep=True) for run in current]

    updated = [run.model_copy(deep=True) for run in current]
    for run in updated:
        if run.status != WorkflowRunStatus.ACTIVE or run.name == target_name:
            continue
        run.status = WorkflowRunStatus.SATISFIED
        run.updated_turn = turn_count
        run.blocked_reason = ""
        run.evidence.append(
            WorkflowEvidence(
                kind=WorkflowStateEventKind.SATISFIED.value,
                ref="tool:checkpoint",
                ok=True,
                summary=f"Checkpoint {decision}; workflow superseded by {target_name}.",
                condition=f"checkpoint_{decision}",
            )
        )

    existing = next((run for run in updated if run.name == target_name), None)
    if existing is None:
        updated.append(WorkflowRunState(name=target_name))
        existing = updated[-1]

    existing.status = WorkflowRunStatus.ACTIVE
    existing.source = WorkflowActivationSource.TRANSITION
    existing.reason = f"checkpoint:{decision}"
    existing.goal_type = ""
    existing.scope = scope
    existing.personas = [node.persona] if node.persona else []
    existing.activated_turn = turn_count
    existing.updated_turn = turn_count
    existing.blocked_reason = ""
    existing.transition_to = list(workflow_transitions(target_name))
    existing.evidence.append(
        WorkflowEvidence(
            kind=WorkflowStateEventKind.ACTIVATED.value,
            ref="tool:checkpoint",
            ok=True,
            summary=f"Checkpoint {decision}; activated {target_name}.",
            condition=f"checkpoint_{decision}",
        )
    )
    return updated
