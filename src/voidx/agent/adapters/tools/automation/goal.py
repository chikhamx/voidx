"""Goal lifecycle control tool for intake initialization and evaluator decisions."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from voidx.agent.domain.automation.goal import GoalSpec as AutonomousGoalSpec
from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.interaction import UserInteraction
from voidx.tooling.domain.schema import model_to_json_schema
from voidx.tooling.domain.ui_events import (
    ChoicePayload,
    GoalSpecDecisionSubmitted,
    GoalSpecPayload,
    GoalSpecPromptShown,
    ToolUiEventPublisher,
)




_INIT_APPROVAL_OPTIONS: list[tuple[str, str, str]] = [
    ("Approve and start", "approved", "Accept the goal spec and start the goal"),
    ("Revise", "revised", "Give feedback so the spec can be revised and re-submitted"),
    ("Cancel", "cancelled", "Do not start this goal"),
]
_INIT_APPROVAL_TIMEOUT_SECONDS = 300.0




async def _request_init_approval(spec: AutonomousGoalSpec, ctx: ToolContext) -> str:
    if ctx.runtime.interaction is None:
        return "auto_approved"
    prompt_id = uuid4().hex
    event_ui_active = _emit_goal_spec_shown(ctx.runtime.events, prompt_id, spec)
    response = await ctx.runtime.interaction(UserInteraction(
        prompt="Goal spec:" if event_ui_active else _init_approval_prompt(spec),
        options=_INIT_APPROVAL_OPTIONS,
        timeout=_INIT_APPROVAL_TIMEOUT_SECONDS,
    ))
    if response.cancelled:
        # Timeout or dismissed prompt auto-approves so autonomous runs are never stuck.
        decision = "auto_approved"
    elif response.free_text:
        decision = f"revise:{response.value}"
    elif response.value == "approved":
        decision = "approved"
    elif response.value == "cancelled":
        decision = "cancelled"
    else:
        decision = "revise:"
    _emit_goal_spec_decision(ctx.runtime.events, prompt_id, decision)
    return decision


def _init_approval_prompt(spec: AutonomousGoalSpec) -> str:
    parts = [
        f"Objective: {spec.objective}",
        f"Acceptance: {spec.acceptance_condition}",
    ]
    if spec.achievement_method:
        parts.append(f"Method: {spec.achievement_method}")
    parts.append(f"Max attempts: {spec.max_attempts}")
    return "\n".join(parts)


def _emit_goal_spec_shown(
    publisher: ToolUiEventPublisher | None,
    prompt_id: str,
    spec: AutonomousGoalSpec,
) -> bool:
    if publisher is None or not publisher.is_running:
        return False
    publisher.emit(GoalSpecPromptShown(
        prompt_id=prompt_id,
        spec=GoalSpecPayload(
            objective=spec.objective,
            acceptance_condition=spec.acceptance_condition,
            achievement_method=spec.achievement_method,
            max_attempts=spec.max_attempts,
        ),
        choices=[
            ChoicePayload(label=label, value=value, description=description)
            for label, value, description in _INIT_APPROVAL_OPTIONS
        ],
    ))
    return True


def _emit_goal_spec_decision(
    publisher: ToolUiEventPublisher | None,
    prompt_id: str,
    decision: str,
) -> None:
    if publisher is None or not publisher.is_running:
        return
    if decision.startswith("revise:"):
        kind, response = "revised", decision.removeprefix("revise:").strip()
    else:
        kind, response = decision, ""
    publisher.emit(GoalSpecDecisionSubmitted(
        prompt_id=prompt_id,
        decision=kind,
        response=response,
    ))


class GoalInitInput(BaseModel):
    objective: str = Field(description="Objective sentence for the autonomous Goal.")
    acceptance_condition: str = Field(description="Verifiable condition that defines completion.")
    achievement_method: str = Field(default="", description="Optional execution guidance.")
    max_attempts: int = Field(default=20, ge=1, le=200, description="Attempt budget.")

    @field_validator("objective", "acceptance_condition", "achievement_method")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class GoalCheckpointInput(BaseModel):
    summary: str = Field(description="Concise summary of the work attempt.")
    evidence: list[str] = Field(default_factory=list, description="Concrete evidence supporting the checkpoint.")
    changed_files: list[str] = Field(default_factory=list, description="Files changed by the work attempt.")
    verification: list[str] = Field(default_factory=list, description="Verification commands or observations.")
    next_hint: str = Field(default="", description="Suggested next action.")
    progress: Literal["none", "partial", "meaningful"] = Field(default="none")

    @field_validator("summary", "next_hint")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("evidence", "changed_files", "verification", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(item).strip() for item in value if str(item).strip()]


class GoalDecisionInput(BaseModel):
    status: Literal["finished", "continue", "blocked"]
    summary: str = Field(description="Concise evaluator decision summary.")
    evidence: list[str] = Field(default_factory=list, description="Evidence used for the decision.")
    reason: str = Field(default="", description="Reason or missing-progress key.")
    next_hint: str = Field(default="", description="Suggested next action when continuing.")
    missing_evidence: list[str] = Field(default_factory=list, description="Evidence still required.")
    progress: Literal["none", "partial", "meaningful"] = Field(default="none")

    @field_validator("summary", "reason", "next_hint")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("evidence", "missing_evidence", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(item).strip() for item in value if str(item).strip()]


def _goal_tool_error(message: str, key: str) -> ToolResult:
    return ToolResult(
        output=message,
        metadata={"error": True, key: False, "goal_protocol_submitted": False},
    )


def _goal_binding(ctx: ToolContext, phase: str) -> tuple[object, str, str, str, int, str, str] | ToolResult:
    runtime = ctx.runtime
    store = getattr(runtime, "goal_store", None)
    generation = str(getattr(runtime, "goal_generation", "") or "").strip()
    parent_session_id = str(
        getattr(runtime, "goal_parent_session_id", "") or ctx.session_id or ""
    ).strip()
    turn_id = str(getattr(runtime, "goal_turn_id", "") or "").strip()
    attempt_number = int(getattr(runtime, "goal_attempt_number", 0) or 0)
    session_attr = {
        "init": "goal_main_session_id",
        "checkpoint": "goal_work_session_id",
        "decision": "goal_evaluator_session_id",
    }[phase]
    session_id = str(getattr(runtime, session_attr, "") or ctx.session_id or "").strip()
    if phase != "init" and attempt_number < 1:
        return _goal_tool_error(
            "Goal protocol binding is missing an attempt number; the call was not submitted.",
            f"goal_{phase}_submitted",
        )
    if store is None or not generation or not parent_session_id or not turn_id or not session_id:
        return _goal_tool_error(
            "Goal protocol durable binding is unavailable; the call was not submitted.",
            f"goal_{phase}_submitted",
        )
    protocol_ids = getattr(runtime, "goal_protocol_ids", None)
    if protocol_ids is None:
        protocol_ids = {}
        runtime.goal_protocol_ids = protocol_ids
    key = f"{phase}:{attempt_number}"
    protocol_id = str(protocol_ids.get(key) or uuid4().hex)
    protocol_ids[key] = protocol_id
    return store, generation, parent_session_id, turn_id, attempt_number, session_id, protocol_id


async def _submit_goal_record(
    record: object,
    store: object,
    key: str,
    *,
    runtime: object,
) -> object | ToolResult:
    try:
        kwargs = {}
        if getattr(record, "phase", "") != "init":
            kwargs = {
                "attempt_id": str(getattr(runtime, "goal_attempt_id", "") or ""),
                "lease_owner": str(getattr(runtime, "goal_lease_owner", "") or ""),
                "fencing_token": int(getattr(runtime, "goal_fencing_token", 0) or 0),
            }
        result = store.submit_goal_protocol(record, **kwargs)
        if hasattr(result, "__await__"):
            result = await result
        return result or record
    except Exception as exc:
        return _goal_tool_error(f"Goal protocol was not durably submitted: {exc}", key)


class GoalInitTool:
    id = "goal_init"
    description = "Submit the approved Goal specification during intake or idle shaping."

    def parameters_schema(self) -> dict:
        schema = model_to_json_schema(GoalInitInput)
        schema["required"] = ["objective", "acceptance_condition"]
        return schema

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = GoalInitInput.model_validate(args)
        except Exception as exc:
            return _goal_tool_error(f"Invalid goal init arguments: {exc}", "goal_init_submitted")
        if ctx.runtime.goal_phase not in {"intake", "idle"} or ctx.runtime.goal_intake is None:
            return ToolResult(
                output="Goal init is only available while shaping a goal; this call was not submitted.",
                metadata={"goal_init_submitted": False, "guidance_only": True},
            )
        binding = _goal_binding(ctx, "init")
        if isinstance(binding, ToolResult):
            return binding
        store, generation, parent, turn_id, _, session_id, protocol_id = binding
        try:
            spec = AutonomousGoalSpec(
                objective=inp.objective,
                acceptance_condition=inp.acceptance_condition,
                achievement_method=inp.achievement_method,
                max_attempts=inp.max_attempts,
                generation=generation,
            )
        except Exception as exc:
            return _goal_tool_error(f"Invalid goal init: {exc}", "goal_init_submitted")
        approval = await _request_init_approval(spec, ctx)
        if approval == "cancelled":
            cancel = getattr(ctx.runtime.goal_intake, "cancel", None)
            if callable(cancel):
                cancel()
            return ToolResult(
                output="Goal init cancelled by the user; the spec was not submitted.",
                metadata={"goal_init_submitted": False, "goal_init_decision": "cancelled"},
            )
        if isinstance(approval, str) and approval.startswith("revise:"):
            return ToolResult(
                output="Revise the goal spec and call goal_init again.",
                metadata={"goal_init_submitted": False, "goal_init_decision": "revised"},
            )
        from voidx.agent.domain.automation.goal import GoalProtocolRecord, GoalSpecSnapshot

        parent_thread_id = str(
            getattr(ctx.runtime, "goal_parent_thread_id", "") or ctx.session_id or ""
        ).strip()
        if not parent_thread_id:
            return _goal_tool_error(
                "Goal init recovery context is missing its parent thread; the call was not submitted.",
                "goal_init_submitted",
            )
        profile_snapshot = getattr(ctx.runtime, "goal_profile_snapshot", {}) or {}
        record = GoalProtocolRecord.submitted(
            protocol_id=protocol_id,
            parent_session_id=parent,
            generation=generation,
            phase="init",
            attempt_number=0,
            turn_id=turn_id,
            session_id=session_id,
            payload=GoalSpecSnapshot.from_spec(
                spec,
                parent_session_id=parent,
                parent_thread_id=parent_thread_id,
                workspace=ctx.workspace,
                profile_snapshot=profile_snapshot,
            ),
        )
        submitted = await _submit_goal_record(
            record,
            store,
            "goal_init_submitted",
            runtime=ctx.runtime,
        )
        if isinstance(submitted, ToolResult):
            return submitted
        accepted = await ctx.runtime.goal_intake.submit_init(spec)
        return ToolResult(
            output="Goal init approved by the user.",
            metadata={
                "goal_init_submitted": True,
                "goal_init_decision": "auto_approved" if approval == "auto_approved" else "approved",
                "goal_spec": accepted.model_dump(mode="json"),
                "protocol_id": submitted.protocol_id,
            },
        )


class GoalCheckpointTool:
    id = "goal_checkpoint"
    description = "Persist a structured checkpoint for the current Goal work attempt."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(GoalCheckpointInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = GoalCheckpointInput.model_validate(args)
        except Exception as exc:
            return _goal_tool_error(f"Invalid goal checkpoint arguments: {exc}", "goal_checkpoint_submitted")
        if not inp.summary:
            return _goal_tool_error(
                "Invalid goal checkpoint: summary is required.",
                "goal_checkpoint_submitted",
            )
        if ctx.runtime.goal_phase != "work":
            return ToolResult(
                output="Goal checkpoints are work-only; this call was not submitted.",
                metadata={"goal_checkpoint_submitted": False, "guidance_only": True},
            )
        binding = _goal_binding(ctx, "checkpoint")
        if isinstance(binding, ToolResult):
            return binding
        store, generation, parent, turn_id, attempt, session_id, protocol_id = binding
        checkpoint_controller = getattr(ctx.runtime, "goal_checkpoint_controller", None)
        if (
            checkpoint_controller is not None
            and checkpoint_controller.final_checkpoint() is not None
        ):
            return ToolResult(
                output="Goal checkpoint already durably recorded for this work attempt; this call was skipped.",
                metadata={
                    "goal_checkpoint_submitted": False,
                    "already_submitted": True,
                    "protocol_id": checkpoint_controller.final_protocol_id(),
                },
            )
        from voidx.agent.domain.automation.goal import GoalProtocolRecord, WorkCheckpoint

        checkpoint = WorkCheckpoint(
            generation=generation,
            attempt_number=attempt,
            summary=inp.summary,
            evidence=tuple(inp.evidence),
            changed_files=tuple(inp.changed_files),
            verification=tuple(inp.verification),
            next_hint=inp.next_hint,
            progress=inp.progress,
            work_turn_id=turn_id,
        )
        record = GoalProtocolRecord.submitted(
            protocol_id=protocol_id,
            parent_session_id=parent,
            generation=generation,
            phase="checkpoint",
            attempt_number=attempt,
            turn_id=turn_id,
            session_id=session_id,
            payload=checkpoint,
        )
        submitted = await _submit_goal_record(
            record,
            store,
            "goal_checkpoint_submitted",
            runtime=ctx.runtime,
        )
        if isinstance(submitted, ToolResult):
            return submitted
        checkpoint_controller = getattr(ctx.runtime, "goal_checkpoint_controller", None)
        if checkpoint_controller is not None:
            await checkpoint_controller.submit_checkpoint(
                checkpoint,
                protocol_id=submitted.protocol_id,
            )
        if isinstance(submitted, ToolResult):
            return submitted
        return ToolResult(
            output="Goal checkpoint durably recorded.",
            metadata={"goal_checkpoint_submitted": True, "protocol_id": submitted.protocol_id},
        )


class GoalDecisionTool:
    id = "goal_decision"
    description = "Persist the evaluator's lifecycle decision for the current Goal attempt."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(GoalDecisionInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = GoalDecisionInput.model_validate(args)
        except Exception as exc:
            return _goal_tool_error(f"Invalid goal decision arguments: {exc}", "goal_decision_submitted")
        if not inp.summary:
            return _goal_tool_error(
                "Invalid goal decision: summary is required.",
                "goal_decision_submitted",
            )
        if ctx.runtime.goal_phase != "evaluator":
            return ToolResult(
                output="Goal decisions are evaluator-only; this call was not submitted.",
                metadata={"goal_decision_submitted": False, "guidance_only": True},
            )
        if ctx.runtime.goal_control is None:
            return _goal_tool_error("Goal evaluator controller is unavailable; the call was not submitted.", "goal_decision_submitted")
        binding = _goal_binding(ctx, "decision")
        if isinstance(binding, ToolResult):
            return binding
        store, generation, parent, turn_id, attempt, session_id, protocol_id = binding
        from voidx.agent.domain.automation.goal import GoalDecision, GoalProtocolRecord

        record = GoalProtocolRecord.submitted(
            protocol_id=protocol_id,
            parent_session_id=parent,
            generation=generation,
            phase="decision",
            attempt_number=attempt,
            turn_id=turn_id,
            session_id=session_id,
            payload=GoalDecision(
                generation=generation,
                attempt_number=attempt,
                status=inp.status,
                summary=inp.summary,
                evidence=tuple(inp.evidence),
                reason=inp.reason,
                next_hint=inp.next_hint,
                missing_evidence=tuple(inp.missing_evidence),
                progress=inp.progress,
            ),
        )
        submitted = await _submit_goal_record(
            record,
            store,
            "goal_decision_submitted",
            runtime=ctx.runtime,
        )
        if isinstance(submitted, ToolResult):
            return submitted
        outcome = {"finished": "completed", "continue": "continue", "blocked": "blocked"}[inp.status]
        decision = await ctx.runtime.goal_control.submit_decision(
            {
                "outcome": outcome,
                "summary": inp.summary,
                "evidence": inp.evidence,
                "next": inp.next_hint,
                "reason": inp.reason,
                "progress": inp.progress,
            },
            protocol_id=submitted.protocol_id,
        )
        return ToolResult(
            output=f"Goal decision durably recorded: {inp.status}.",
            metadata={
                "goal_decision_submitted": True,
                "protocol_id": submitted.protocol_id,
                "outcome": decision.outcome,
            },
        )


__all__ = ["GoalCheckpointTool", "GoalDecisionTool", "GoalInitTool"]
