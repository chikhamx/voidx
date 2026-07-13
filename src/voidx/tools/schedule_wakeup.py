"""Tool for rescheduling dynamic /loop wakeups."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema

_MIN_DELAY = 60.0
_MAX_DELAY = 3600.0


class ScheduleWakeupInput(BaseModel):
    delay_seconds: float | None = Field(
        default=None,
        description="Seconds until the next dynamic loop iteration. Min 60, max 3600. Optional when stop=true.",
    )
    stop: bool = Field(
        default=False,
        description="Set true to stop the current loop instead of scheduling the next wakeup.",
    )


class ScheduleWakeupTool(BaseTool):
    id = "schedule_wakeup"
    description = (
        "Reschedule the next iteration of a self-paced /loop. "
        "Call this at the end of each loop iteration to pick when the next one runs. "
        "Pass stop=true to end the loop."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(ScheduleWakeupInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = ScheduleWakeupInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})

        manager = ctx.loop_manager
        if manager is None:
            return ToolResult(
                output="No active /loop manager is available in this tool context.",
                metadata={"error": True, "loop_active": False},
            )

        status = manager.status()
        if status is None:
            return ToolResult(
                output="No active /loop is running.",
                metadata={"error": True, "loop_active": False},
            )

        mode = status.get("mode", "unknown")
        if inp.stop:
            manager.schedule_wakeup(None, stop=True)
            return ToolResult(
                output="Stopped the active /loop.",
                metadata={"stopped": True, "scheduled": False, "loop_active": False, "mode": mode},
            )

        if mode != "dynamic":
            return ToolResult(
                output="schedule_wakeup can only reschedule a dynamic /loop. Use stop=true to stop a fixed loop.",
                metadata={"error": True, "loop_active": True, "mode": mode},
            )
        if inp.delay_seconds is None:
            return ToolResult(
                output="delay_seconds is required unless stop=true.",
                metadata={"error": True, "loop_active": True, "mode": mode},
            )
        delay = float(inp.delay_seconds)
        if delay < _MIN_DELAY or delay > _MAX_DELAY:
            return ToolResult(
                output="delay_seconds must be between 60 and 3600 seconds.",
                metadata={"error": True, "loop_active": True, "mode": mode},
            )

        manager.schedule_wakeup(delay)
        return ToolResult(
            output=f"Scheduled the next /loop wakeup in {delay:g} seconds.",
            metadata={
                "scheduled": True,
                "stopped": False,
                "delay_seconds": delay,
                "loop_active": True,
                "mode": mode,
            },
        )
