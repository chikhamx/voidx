"""TaskStatus tool — check worker-role progress. Claude Code aligned."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult


class TaskStatusInput(BaseModel):
    task_id: str | None = Field(
        default=None,
        description="Specific task ID to check. If omitted, lists all tasks."
    )


class TaskStatusTool(BaseTool):
    id = "task_status"
    description = (
        "Check child-agent task status. Returns status (pending/running/completed/error), "
        "current step, elapsed time, and recent output preview. "
        "Without task_id, lists all tasks."
    )

    def __init__(self, tracker=None):
        super().__init__()
        self._tracker = tracker

    def parameters_schema(self) -> dict:
        return model_to_json_schema(TaskStatusInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = TaskStatusInput.model_validate(args)

        if not self._tracker:
            return ToolResult(output="Task tracker not available.")

        if inp.task_id:
            task = self._tracker.get(inp.task_id)
            if not task:
                return ToolResult(output=f"Task not found: {inp.task_id}")
            return ToolResult(
                title=f"Task {inp.task_id}: {task.status}",
                output=self._tracker.format_status(),
                metadata={
                    "task_id": task.id, "agent": task.agent,
                    "status": task.status, "step": task.step,
                },
            )

        output = self._tracker.format_status()
        running = self._tracker.list_running()
        return ToolResult(
            title=f"Tasks: {len(running)} running, {len(self._tracker.list_all())} total",
            output=output,
            metadata={"running": len(running), "total": len(self._tracker._tasks)},
        )
