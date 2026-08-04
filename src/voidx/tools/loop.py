"""Tool for runtime-backed /loop lifecycle: start declares intent, commit submits the iteration decision.

The loop never ends on its own: only the user ends it via /loop stop or by
closing voidx, so the model-facing surface exposes no terminal outcome.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from voidx.runtime.task_state import GoalSpec, LoopSpec, ToolStatePatch
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    UserInteraction,
    keep_tool_args,
    model_to_json_schema,
)


class LoopDecisionInput(BaseModel):
    operation: Literal["start", "commit", "init"] = Field(
        default="commit",
        description=(
            "start declares the iteration goal; commit submits the iteration decision "
            "(requires outcome/summary); init submits a LoopSpec for user approval "
            "(idle phase only, requires prompt)."
        ),
    )
    goal: str = Field(default="", description="Iteration goal. Required for operation=start.")
    prompt: str = Field(
        default="",
        description="For operation=init: the loop prompt/goal. Required for init.",
    )
    interval_seconds: float | None = Field(
        default=None,
        description="For operation=init: fixed interval in seconds. Omit for dynamic mode.",
    )
    outcome: Literal["continue"] | None = Field(
        default=None,
        description=(
            "Iteration outcome. Required for operation=commit; the only valid value is "
            "'continue', which schedules the next wakeup. The loop only ends when the user "
            "stops it, so finishing this iteration's work is NOT a reason to end the loop."
        ),
    )
    summary: str = Field(default="", description="Concise durable summary of this loop iteration.")
    progress: Literal["none", "partial", "meaningful"] = Field(
        default="none", description="none, partial, or meaningful."
    )
    next_delay_seconds: float | None = Field(default=None)
    reason: str = Field(default="")


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


class LoopTool(BaseTool):
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
            return await _submit_init(inp, ctx)
        controller = ctx.loop_controller
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
        return self._decision_result(committed, controller)

    @staticmethod
    def _decision_result(committed, controller) -> ToolResult:
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



_LOOP_INIT_APPROVAL_OPTIONS: list[tuple[str, str, str]] = [
    ("Approve and start", "approved", "Accept the loop spec and start the loop"),
    ("Revise", "revised", "Give feedback so the spec can be revised and re-submitted"),
    ("Cancel", "cancelled", "Do not start this loop"),
]
_LOOP_INIT_APPROVAL_TIMEOUT_SECONDS = 300.0


async def _submit_init(inp: LoopDecisionInput, ctx: ToolContext) -> ToolResult:
    controller = ctx.loop_intake_controller
    if ctx.loop_phase != "idle" or controller is None:
        return ToolResult(
            output="Loop init is only available while shaping a loop; this call was not submitted.",
            metadata={"loop_init_submitted": False, "guidance_only": True},
        )
    prompt = inp.prompt.strip()
    if not prompt:
        return ToolResult(
            output="operation=init requires a non-empty prompt.",
            metadata={"error": True},
        )
    try:
        spec = LoopSpec(prompt=prompt, interval_seconds=inp.interval_seconds)
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
                "Revise the spec accordingly and call loop(operation=\"init\") again with the updated fields."
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
        },
    )


async def _request_loop_init_approval(spec: LoopSpec, ctx: ToolContext) -> str:
    if ctx.interact is None:
        return "auto_approved"
    response = await ctx.interact(UserInteraction(
        prompt=_loop_init_approval_prompt(spec),
        options=_LOOP_INIT_APPROVAL_OPTIONS,
        timeout=_LOOP_INIT_APPROVAL_TIMEOUT_SECONDS,
    ))
    if response.cancelled:
        return "auto_approved"
    if response.free_text:
        return f"revise:{response.value}"
    if response.value == "approved":
        return "approved"
    if response.value == "cancelled":
        return "cancelled"
    return "revise:"


def _loop_init_approval_prompt(spec: LoopSpec) -> str:
    parts = [f"Prompt: {spec.prompt}"]
    if spec.interval_seconds is not None:
        parts.append(f"Interval: {spec.interval_seconds:g}s (fixed)")
    else:
        parts.append("Interval: dynamic")
    return "\n".join(parts)
