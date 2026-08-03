"""Goal lifecycle control tool for intake initialization and evaluator decisions."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from voidx.runtime.goal import GoalSpec as AutonomousGoalSpec
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    UserInteraction,
    model_to_json_schema,
)


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


_INIT_APPROVAL_OPTIONS: list[tuple[str, str, str]] = [
    ("Approve and start", "approved", "Accept the goal spec and start the goal"),
    ("Revise", "revised", "Give feedback so the spec can be revised and re-submitted"),
    ("Cancel", "cancelled", "Do not start this goal"),
]
_INIT_APPROVAL_TIMEOUT_SECONDS = 300.0


async def _submit_init(inp: GoalInput, ctx: ToolContext) -> ToolResult:
    controller = ctx.goal_intake_controller
    if ctx.goal_phase not in {"intake", "idle"} or controller is None:
        return ToolResult(
            output="Goal init is only available while shaping a goal; this call was not submitted.",
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
    approval = await _request_init_approval(spec, ctx)
    if approval == "cancelled":
        cancel = getattr(controller, "cancel", None)
        if callable(cancel):
            cancel()
        return ToolResult(
            output="Goal init cancelled by the user; the spec was not submitted. Intake is over.",
            metadata={"goal_init_submitted": False, "goal_init_decision": "cancelled"},
        )
    if isinstance(approval, str) and approval.startswith("revise:"):
        feedback = approval.removeprefix("revise:").strip()
        return ToolResult(
            output=(
                "The user requested changes to the goal spec and it was not submitted. "
                f"Feedback: {feedback or '(no details)'}. "
                "Revise the spec accordingly and call goal(op=\"init\") again with the updated fields."
            ),
            metadata={"goal_init_submitted": False, "goal_init_decision": "revised"},
        )
    submitted = await controller.submit_init(spec)
    auto = approval == "auto_approved"
    return ToolResult(
        output="Goal init approved by the user." if not auto else "Goal init auto-approved (no user response).",
        metadata={
            "goal_init_submitted": True,
            "goal_init_decision": "auto_approved" if auto else "approved",
            "goal_spec": submitted.model_dump(mode="json"),
        },
    )


async def _request_init_approval(spec: AutonomousGoalSpec, ctx: ToolContext) -> str:
    if ctx.interact is None:
        return "auto_approved"
    prompt_id = uuid4().hex
    event_ui_active = _emit_goal_spec_shown(prompt_id, spec)
    response = await ctx.interact(UserInteraction(
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
    _emit_goal_spec_decision(prompt_id, decision)
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


def _emit_goal_spec_shown(prompt_id: str, spec: AutonomousGoalSpec) -> bool:
    try:
        from voidx.ui.output.events import ui_events
        from voidx.ui.output.events.schema import (
            GoalSpecChoicePayload,
            GoalSpecPayload,
            GoalSpecPromptShown,
        )
    except ImportError:
        return False
    if not ui_events.is_running:
        return False
    ui_events.emit_direct(GoalSpecPromptShown(
        prompt_id=prompt_id,
        spec=GoalSpecPayload(
            objective=spec.objective,
            acceptance_condition=spec.acceptance_condition,
            achievement_method=spec.achievement_method,
            max_attempts=spec.max_attempts,
        ),
        choices=[
            GoalSpecChoicePayload(label=label, value=value, description=description)
            for label, value, description in _INIT_APPROVAL_OPTIONS
        ],
    ))
    return True


def _emit_goal_spec_decision(prompt_id: str, decision: str) -> None:
    try:
        from voidx.ui.output.events import ui_events
        from voidx.ui.output.events.schema import GoalSpecDecisionSubmitted
    except ImportError:
        return
    if not ui_events.is_running:
        return
    if decision.startswith("revise:"):
        kind, response = "revised", decision.removeprefix("revise:").strip()
    else:
        kind, response = decision, ""
    ui_events.emit_direct(GoalSpecDecisionSubmitted(
        prompt_id=prompt_id,
        decision=kind,
        response=response,
    ))


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
