"""Tool for runtime-backed /loop lifecycle: start declares intent, commit submits the iteration decision.

The loop never ends on its own: only the user ends it via /loop stop or by
closing voidx, so the model-facing surface exposes no terminal outcome.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    keep_tool_args,
    model_to_json_schema,
)


class LoopDecisionInput(BaseModel):
    operation: Literal["start", "commit"] = Field(
        default="commit",
        description=(
            "start declares the iteration goal; commit submits the iteration decision "
            "(requires outcome/summary)."
        ),
    )
    goal: str = Field(default="", description="Iteration goal. Required for operation=start.")
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
        controller = ctx.loop_controller
        if controller is None:
            return ToolResult(
                output="No active runtime-backed /loop controller is available in this tool context.",
                metadata={"error": True, "loop_active": False},
            )
        args = _normalize_loop_args(args)
        try:
            inp = LoopDecisionInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})

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
            metadata={"operation": "start", "goal": inp.goal.strip()},
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
