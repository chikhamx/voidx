"""Tool for submitting runtime-backed /loop lifecycle decisions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema


class LoopUpdateInput(BaseModel):
    outcome: Literal["continue", "completed", "blocked", "needs_user", "failed", "stop"] = Field(
        description="Loop lifecycle outcome."
    )
    summary: str = Field(description="Concise durable summary of this loop iteration.")
    progress: Literal["none", "partial", "meaningful"] = Field(
        default="none", description="none, partial, or meaningful."
    )
    next_delay_seconds: float | None = Field(default=None)
    reason: str = Field(default="")


class LoopUpdateTool(BaseTool):
    id = "loop_update"
    description = (
        "Submit the lifecycle decision for a runtime-backed /loop attempt. "
        "Use outcome=continue to schedule another wakeup, completed/stop for terminal outcomes, "
        "or blocked/needs_user when the automatic loop cannot proceed."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LoopUpdateInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        controller = ctx.loop_controller
        if controller is None:
            return ToolResult(
                output="No active runtime-backed /loop controller is available in this tool context.",
                metadata={"error": True, "loop_active": False},
            )
        try:
            inp = LoopUpdateInput.model_validate(args)
            decision = inp.model_dump(mode="json")
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})

        committed = await controller.submit_decision(decision)
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
