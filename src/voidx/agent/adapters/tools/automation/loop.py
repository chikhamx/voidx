"""Tool for runtime-backed /loop lifecycle: start declares intent, commit submits the iteration decision.

The loop never ends on its own: only the user ends it via /loop stop or by
closing voidx, so the model-facing surface exposes no terminal outcome.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, autonomous_init_decision
from voidx.agent.domain.automation.loop import LoopSpec
from voidx.agent.domain.task.state import GoalSpec, ToolStatePatch
from voidx.tooling.domain.arguments import keep_tool_args
from voidx.tooling.domain.interaction import InteractionRequest, UserInteraction
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.schema import model_to_json_schema
from voidx.tooling.domain.ui_events import (
    ChoicePayload,
    LoopSpecDecisionSubmitted,
    LoopSpecPayload,
    LoopSpecPromptShown,
    ToolUiEventPublisher,
)


def _validate_positive_int(field_name: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer >= 1")
    if value < 1:
        raise ValueError(f"{field_name} must be >= 1")
    return value


class LoopInitInput(BaseModel):
    goal: str = Field(
        default="",
        description="The loop goal/task instructions describing what the autonomous loop should do.",
    )
    prompt: str = Field(
        default="",
        description="The loop prompt/goal describing what the autonomous loop should do.",
    )
    interval_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Fixed interval in whole seconds, at least 1; omit for dynamic mode.",
    )

    @model_validator(mode="before")
    @classmethod
    def sync_goal_and_prompt(cls, data: Any) -> Any:
        if isinstance(data, dict):
            text = str(data.get("goal") or data.get("prompt") or "").strip()
            if text:
                data["goal"] = text
                data["prompt"] = text
        return data

    @field_validator("goal", "prompt")
    @classmethod
    def require_goal(cls, value: str) -> str:
        val = value.strip()
        if not val:
            raise ValueError("goal must not be empty")
        return val

    @field_validator("interval_seconds")
    @classmethod
    def check_interval(cls, value: Any) -> int | None:
        return _validate_positive_int("interval_seconds", value)


class LoopStartInput(BaseModel):
    goal: str = Field(
        description="Iteration micro-goal. Declares what this specific iteration aims to accomplish.",
    )

    @field_validator("goal")
    @classmethod
    def require_goal(cls, value: str) -> str:
        goal = value.strip()
        if not goal:
            raise ValueError("goal must not be empty")
        return goal


class LoopCommitInput(BaseModel):
    outcome: Literal["continue"] = Field(
        default="continue",
        description="Required for commit; 'continue' schedules the next wakeup. Finishing an iteration does not stop the loop; stopping remains user-controlled.",
    )
    summary: str = Field(
        description="Concise durable summary of what was accomplished in this iteration.",
    )
    progress: Literal["none", "partial", "meaningful"] = Field(
        default="none",
        description="Progress toward the loop goal: none, partial, or meaningful.",
    )
    next_delay_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Optional custom delay in whole seconds until next iteration (dynamic mode only, integer >= 1).",
    )
    reason: str = Field(
        default="",
        description="Optional rationale for delay or progress assessment.",
    )

    @field_validator("summary")
    @classmethod
    def require_summary(cls, value: str) -> str:
        summary = value.strip()
        if not summary:
            raise ValueError("summary must not be empty")
        return summary

    @field_validator("next_delay_seconds")
    @classmethod
    def check_delay(cls, value: Any) -> int | None:
        return _validate_positive_int("next_delay_seconds", value)


class LoopDecisionInput(BaseModel):
    operation: Literal["start", "commit", "init"] = Field(
        default="commit",
        description="start declares a goal; commit records outcome/summary; init submits a prompt for LoopSpec approval in idle phase only.",
    )
    goal: str = Field(default="", description="Iteration goal. Required for operation=start.")
    prompt: str = Field(
        default="",
        description="For operation=init: the loop prompt/goal. Required for init.",
    )
    interval_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Fixed interval in whole seconds, at least 1; omit for dynamic mode.",
    )
    outcome: Literal["continue"] | None = Field(
        default=None,
        description="Required for commit; 'continue' schedules the next wakeup. Finishing an iteration does not stop the loop; stopping remains user-controlled.",
    )
    summary: str = Field(default="", description="Concise durable summary of this loop iteration.")
    progress: Literal["none", "partial", "meaningful"] = Field(
        default="none",
        description=(
            "Progress toward the loop goal: none, partial, or meaningful. Report it "
            "honestly — consecutive 'none' iterations auto-pause the loop for user review."
        ),
    )
    next_delay_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Delay in seconds (integer only).",
    )
    reason: str = Field(default="")

    @field_validator("interval_seconds")
    @classmethod
    def check_interval(cls, value: Any) -> int | None:
        return _validate_positive_int("interval_seconds", value)

    @field_validator("next_delay_seconds")
    @classmethod
    def check_delay(cls, value: Any) -> int | None:
        return _validate_positive_int("next_delay_seconds", value)


def _normalize_loop_args(args: Any) -> Any:
    if not isinstance(args, dict):
        return args
    operation = str(args.get("operation") or "commit").strip().lower()
    if operation == "start":
        return keep_tool_args(args, {"operation", "goal"})
    if operation == "init":
        return keep_tool_args(args, {"operation", "prompt", "interval_seconds"})
    if operation == "commit":
        return keep_tool_args(
            args,
            {"operation", "outcome", "summary", "progress", "next_delay_seconds", "reason"},
        )
    return args


def _decision_result_from_committed(committed, controller) -> ToolResult:
    spec = getattr(controller, "spec", None)
    mode = spec.mode.value if spec is not None else "unknown"
    terminal = committed.outcome in {"completed", "failed", "stop"}
    next_delay = None if terminal else committed.next_delay_seconds
    return ToolResult(
        output=f"Loop decision recorded: {committed.outcome}.",
        metadata={
            "outcome": committed.outcome,
            "summary": committed.summary,
            "progress": committed.progress,
            "reason": committed.reason,
            "next_delay_seconds": next_delay,
            "terminal": terminal,
            "mode": mode,
            "fixed": mode == "fixed",
        },
    )


class LoopInitTool:
    id = "loop_init"
    description = (
        "Submit a LoopSpec for user approval during shaping/idle phase. "
        "Requires prompt, and optional integer interval_seconds for fixed intervals."
    )

    def parameters_schema(self) -> dict:
        schema = model_to_json_schema(LoopInitInput)
        schema["required"] = ["goal"]
        return schema

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = LoopInitInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        return await _submit_init(inp.goal, inp.interval_seconds, ctx)


class LoopStartTool:
    id = "loop_start"
    description = "Declare the specific micro-goal for this loop iteration."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LoopStartInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = LoopStartInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        controller = ctx.runtime.loop_control
        if controller is None:
            return ToolResult(
                output="No active runtime-backed /loop controller is available in this tool context.",
                metadata={"error": True, "loop_active": False},
            )
        return ToolResult(
            output=f"Loop iteration started: {inp.goal.strip()}",
            metadata={
                "operation": "start",
                "goal": inp.goal.strip(),
                "state_patch": ToolStatePatch(
                    goal=GoalSpec(desc=inp.goal.strip())
                ).model_dump(mode="json", exclude_unset=True),
            },
        )


class LoopCommitTool:
    id = "loop_commit"
    description = (
        "Submit the iteration decision, durable summary, and schedule the next wakeup. "
        "The loop only ends when the user stops it — outcome must be 'continue'."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LoopCommitInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = LoopCommitInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        controller = ctx.runtime.loop_control
        if controller is None:
            return ToolResult(
                output="No active runtime-backed /loop controller is available in this tool context.",
                metadata={"error": True, "loop_active": False},
            )
        decision = {
            "outcome": inp.outcome,
            "summary": inp.summary,
            "progress": inp.progress,
            "next_delay_seconds": inp.next_delay_seconds,
            "reason": inp.reason,
        }
        try:
            committed = await controller.submit_decision(decision)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        return _decision_result_from_committed(committed, controller)


class LoopTool:
    id = "loop"
    description = (
        "Runtime-backed /loop lifecycle control. The loop only ends when the user runs "
        "/loop stop or closes voidx — never end or pause it yourself. Call operation='start' "
        "with goal at iteration start. Call operation='commit' with outcome='continue' and "
        "a summary to submit the iteration decision and schedule the next wakeup — use it "
        "even when this iteration's work is done or you are waiting on something."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LoopDecisionInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        args = _normalize_loop_args(args)
        try:
            inp = LoopDecisionInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})

        if inp.operation == "init":
            return await _submit_init(inp.prompt, inp.interval_seconds, ctx)
        controller = ctx.runtime.loop_control
        if controller is None:
            return ToolResult(
                output="No active runtime-backed /loop controller is available in this tool context.",
                metadata={"error": True, "loop_active": False},
            )
        if inp.operation == "start":
            return self._start(inp, controller)
        return await self._commit(inp, controller)

    def _start(self, inp: LoopDecisionInput, controller) -> ToolResult:
        if not inp.goal.strip():
            return ToolResult(
                output="operation=start requires a non-empty goal.",
                metadata={"error": True},
            )
        return ToolResult(
            output=f"Loop iteration started: {inp.goal.strip()}",
            metadata={
                "operation": "start",
                "goal": inp.goal.strip(),
                "state_patch": ToolStatePatch(
                    goal=GoalSpec(desc=inp.goal.strip())
                ).model_dump(mode="json", exclude_unset=True),
            },
        )

    async def _commit(self, inp: LoopDecisionInput, controller) -> ToolResult:
        if inp.outcome is None:
            return ToolResult(
                output="operation=commit requires outcome and summary.",
                metadata={"error": True},
            )
        decision = {
            "outcome": inp.outcome,
            "summary": inp.summary,
            "progress": inp.progress,
            "next_delay_seconds": inp.next_delay_seconds,
            "reason": inp.reason,
        }
        try:
            committed = await controller.submit_decision(decision)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        return _decision_result_from_committed(committed, controller)

    @staticmethod
    def _decision_result(committed, controller) -> ToolResult:
        return _decision_result_from_committed(committed, controller)


_LOOP_INIT_APPROVAL_OPTIONS: list[tuple[str, str, str]] = [
    ("Approve and start", "approved", "Accept the loop spec and start the loop"),
    ("Revise", "revised", "Give feedback so the spec can be revised and re-submitted"),
    ("Cancel", "cancelled", "Do not start this loop"),
]
_LOOP_INIT_APPROVAL_TIMEOUT_SECONDS = 300.0


async def _submit_init(prompt: str, interval_seconds: int | None, ctx: ToolContext) -> ToolResult:
    controller = ctx.runtime.loop_intake
    if ctx.runtime.loop_phase != "idle" or controller is None:
        return ToolResult(
            output="Loop init is only available while shaping a loop; this call was not submitted.",
            metadata={"loop_init_submitted": False, "guidance_only": True},
        )
    prompt = prompt.strip()
    if not prompt:
        return ToolResult(
            output="operation=init requires a non-empty prompt.",
            metadata={"error": True},
        )
    try:
        spec = LoopSpec(prompt=prompt, interval_seconds=interval_seconds)
    except Exception as exc:
        return ToolResult(output=f"Invalid loop init: {exc}", metadata={"error": True})
    approval = await _request_loop_init_approval(spec, ctx)
    if approval == "cancelled":
        controller.cancel()
        return ToolResult(
            output="Loop init cancelled by the user; the spec was not submitted. Intake is over.",
            metadata={"loop_init_submitted": False, "loop_init_decision": "cancelled"},
        )
    if isinstance(approval, str) and approval.startswith("revise:"):
        feedback = approval.removeprefix("revise:").strip()
        return ToolResult(
            output=(
                "The user requested changes to the loop spec and it was not submitted. "
                f"Feedback: {feedback or '(no details)'}. "
                "Revise the spec accordingly and call loop_init again with the updated fields."
            ),
            metadata={"loop_init_submitted": False, "loop_init_decision": "revised"},
        )
    submitted = await controller.submit_init(spec)
    auto = approval == "auto_approved"
    return ToolResult(
        output="Loop init approved by the user." if not auto else "Loop init auto-approved (no user response).",
        metadata={
            "loop_init_submitted": True,
            "loop_init_decision": "auto_approved" if auto else "approved",
            "loop_spec": submitted.model_dump(mode="json"),
            "state_patch": ToolStatePatch(
                goal=GoalSpec(desc=prompt.strip())
            ).model_dump(mode="json", exclude_unset=True),
        },
    )


async def _request_loop_init_approval(spec: LoopSpec, ctx: ToolContext) -> str:
    if ctx.runtime.autonomous_requester is not None:
        return await autonomous_init_decision(ctx.runtime, InteractionRequest(
            interaction_id="loop-init",
            **{key: ctx.runtime.interaction_identity[key] for key in ("session_id", "thread_id", "turn_id")},
            input_kind="choice", purpose="loop", prompt=_loop_init_approval_prompt(spec),
            choices=[ChoicePayload(label=label, value=value, description=description)
                     for label, value, description in _LOOP_INIT_APPROVAL_OPTIONS],
            allow_free_text=True, timeout=_LOOP_INIT_APPROVAL_TIMEOUT_SECONDS,
            loop=LoopSpecPayload(prompt=spec.prompt, interval_seconds=spec.interval_seconds),
        ))
    if ctx.runtime.interaction is None:
        return "auto_approved"
    prompt_id = uuid4().hex
    event_ui_active = _emit_loop_spec_shown(ctx.runtime.events, prompt_id, spec)
    response = await ctx.runtime.interaction(UserInteraction(
        prompt="Loop spec:" if event_ui_active else _loop_init_approval_prompt(spec),
        options=_LOOP_INIT_APPROVAL_OPTIONS,
        timeout=_LOOP_INIT_APPROVAL_TIMEOUT_SECONDS,
    ))
    if response.cancelled:
        decision = "auto_approved"
    elif response.free_text:
        decision = f"revise:{response.value}"
    elif response.value == "approved":
        decision = "approved"
    elif response.value == "cancelled":
        decision = "cancelled"
    else:
        decision = "revise:"
    _emit_loop_spec_decision(ctx.runtime.events, prompt_id, decision)
    return decision


def _emit_loop_spec_shown(
    publisher: ToolUiEventPublisher | None,
    prompt_id: str,
    spec: LoopSpec,
) -> bool:
    if publisher is None or not publisher.is_running:
        return False
    publisher.emit(LoopSpecPromptShown(
        prompt_id=prompt_id,
        spec=LoopSpecPayload(
            prompt=spec.prompt,
            interval_seconds=spec.interval_seconds,
        ),
        choices=[
            ChoicePayload(label=label, value=value, description=description)
            for label, value, description in _LOOP_INIT_APPROVAL_OPTIONS
        ],
    ))
    return True


def _emit_loop_spec_decision(
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
    publisher.emit(LoopSpecDecisionSubmitted(
        prompt_id=prompt_id,
        decision=kind,
        response=response,
    ))


def _loop_init_approval_prompt(spec: LoopSpec) -> str:
    parts = [f"Prompt: {spec.prompt}"]
    if spec.interval_seconds is not None:
        parts.append(f"Interval: {spec.interval_seconds:d}s (fixed)")
    else:
        parts.append("Interval: dynamic")
    return "\n".join(parts)


__all__ = [
    "LoopCommitInput",
    "LoopCommitTool",
    "LoopDecisionInput",
    "LoopInitInput",
    "LoopInitTool",
    "LoopStartInput",
    "LoopStartTool",
    "LoopTool",
]
