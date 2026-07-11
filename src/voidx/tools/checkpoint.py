"""Structured implementation plan approval tool."""

from __future__ import annotations

import json
from uuid import uuid4

from pydantic import BaseModel, Field

from voidx.runtime import GoalSpec, IntentResolution, PlanResolution, TaskIntent, ToolStatePatch
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
    goal: str = Field(description="Stable overall objective for the current task. Keep it short, sharp, and clear.")
    steps: list[str] = Field(default_factory=list, description="Ordered implementation steps; each step should be one small action.")
    affected_files: list[str] = Field(
        default_factory=list,
        description="Files that may be created, modified, moved, or deleted by the plan.",
    )
    risks: list[str] = Field(default_factory=list, description="Risks, edge cases, or trade-offs the user should approve.")


class PlanCheckpointResult(BaseModel):
    goal: str
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
        "Present a concrete implementation plan as an approval gate before file edits, "
        "write-capable commands, or delegated implementation. no code changes occur in this tool. "
        "The user can approve, request a design document first, modify scope, or reject. "
        "Later tool calls in the same response are deferred until the decision updates runtime state."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(PlanCheckpointInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = PlanCheckpointInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", summary="plan: invalid arguments", metadata={"error": True})
        if ctx.interact is None:
            return ToolResult(
                title="plan: approval unavailable",
                output=(
                    "Plan approval is not available in this runtime. "
                    f"Do not implement without explicit user approval: {inp.goal}"
                ),
                summary="plan: approval unavailable",
                metadata={"plan_decision": "interaction_unavailable", "blocked": True},
            )

        checkpoint_id = uuid4().hex
        event_ui_active = _emit_checkpoint_shown(checkpoint_id, inp)
        response = await ctx.interact(UserInteraction(
            prompt="Plan:" if event_ui_active else _build_prompt(inp),
            options=_CHECKPOINT_OPTIONS,
            timeout=120.0,
        ))
        decision = _DECISION_MAP.get(response.value, "modified")
        if response.cancelled or decision == "rejected":
            if response.cancelled:
                _emit_checkpoint_decision(
                    checkpoint_id,
                    decision="rejected",
                    label="",
                    response="no response; treated as rejected",
                )
            else:
                _emit_checkpoint_decision(
                    checkpoint_id,
                    decision="rejected",
                    label=_choice_label("rejected"),
                    response=_choice_label("rejected"),
                )
            return _decision_result(inp, decision="rejected")
        if response.free_text:
            _emit_checkpoint_decision(
                checkpoint_id,
                decision="modified",
                label="Other...",
                response=response.value.strip(),
                was_custom_input=True,
            )
            return _decision_result(inp, decision="modified", modified_scope=response.value.strip())
        if decision == "modified":
            scope_response = await ctx.interact(UserInteraction(
                prompt="Describe the modified scope:",
                timeout=120.0,
            ))
            modified_scope = "" if scope_response.cancelled else scope_response.value.strip()
            _emit_checkpoint_decision(
                checkpoint_id,
                decision="modified",
                label=_choice_label("modified"),
                response=modified_scope,
                was_custom_input=not scope_response.cancelled,
            )
            return _decision_result(inp, decision="modified", modified_scope=modified_scope)
        if decision == "approved":
            _emit_checkpoint_decision(
                checkpoint_id,
                decision="approved",
                label=_choice_label("approved"),
                response=_choice_label("approved"),
            )
            return _decision_result(
                inp,
                decision="approved",
                workflow_runs=ctx.workflow_runs,
                turn_count=ctx.turn_count,
            )
        if decision == "needs_doc":
            _emit_checkpoint_decision(
                checkpoint_id,
                decision="needs_doc",
                label=_choice_label("needs_doc"),
                response=_choice_label("needs_doc"),
            )
            return _decision_result(
                inp,
                decision="needs_doc",
                workflow_runs=ctx.workflow_runs,
                turn_count=ctx.turn_count,
            )
        _emit_checkpoint_decision(
            checkpoint_id,
            decision="modified",
            label=_choice_label("modified"),
            response=response.value.strip(),
            was_custom_input=bool(response.free_text),
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
        scope = inp.goal.strip()
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc=scope),
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
        scope = inp.goal.strip()
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc=scope),
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
        scope = modified_scope or inp.goal.strip()
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc=scope),
        )
        next_step_hint = ""
    else:
        patch = ToolStatePatch(
            intent=IntentResolution(type=TaskIntent.CODING),
        )
        next_step_hint = ""

    result = PlanCheckpointResult(
        goal=inp.goal,
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
    parts = [f"Goal: {inp.goal}"]
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


def _choice_label(value: str) -> str:
    for label, option_value, _description in _CHECKPOINT_OPTIONS:
        if option_value == value:
            return label
    return value


def _emit_checkpoint_shown(checkpoint_id: str, inp: PlanCheckpointInput) -> bool:
    try:
        from voidx.ui.output.events import ui_events
        from voidx.ui.output.events.schema import (
            CheckpointChoicePayload,
            CheckpointPlanPayload,
            CheckpointPromptShown,
        )
    except ImportError:
        return False
    if not ui_events.is_running:
        return False
    ui_events.emit_direct(CheckpointPromptShown(
        checkpoint_id=checkpoint_id,
        plan=CheckpointPlanPayload(
            goal=inp.goal,
            steps=inp.steps,
            affected_files=inp.affected_files,
            risks=inp.risks,
        ),
        choices=[
            CheckpointChoicePayload(label=label, value=value, description=description)
            for label, value, description in _CHECKPOINT_OPTIONS
        ],
    ))
    return True


def _emit_checkpoint_decision(
    checkpoint_id: str,
    *,
    decision: str,
    label: str,
    response: str,
    was_custom_input: bool = False,
) -> None:
    try:
        from voidx.ui.output.events import ui_events
        from voidx.ui.output.events.schema import CheckpointDecisionSubmitted
    except ImportError:
        return
    if not ui_events.is_running:
        return
    ui_events.emit_direct(CheckpointDecisionSubmitted(
        checkpoint_id=checkpoint_id,
        decision=decision,
        label=label,
        response=response,
        was_custom_input=was_custom_input,
    ))


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
