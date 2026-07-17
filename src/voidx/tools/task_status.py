"""TaskStatus tool — check worker-persona progress. Claude Code aligned."""

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
        "Inspect tasks recorded by the child-agent tracker. With task_id, returns that "
        "task's current tracked status; without task_id, lists all tracked tasks."
    )

    def __init__(self, tracker=None):
        super().__init__()
        self._tracker = tracker

    def parameters_schema(self) -> dict:
        return model_to_json_schema(TaskStatusInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = TaskStatusInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})

        if not self._tracker:
            return ToolResult(
                title="Task: Error",
                output="Task tracker not available.",
                summary="error: tracker unavailable",
                metadata={"error": True, "reason": "no_tracker"},
            )

        if inp.task_id:
            task = self._tracker.get(inp.task_id)
            if not task:
                return ToolResult(
                    title="Task: Error",
                    output=f"Task not found: {inp.task_id}",
                    summary=f"error: task not found: {inp.task_id}",
                    metadata={"error": True, "reason": "not_found", "task_id": inp.task_id},
                )
            return ToolResult(
                title=f"Task {inp.task_id}: {task.status}",
                output=self._tracker.format_status(),
                summary=f"task {inp.task_id}: {task.status}",
                metadata={
                    "task_id": task.id, "agent": task.agent,
                    "status": task.status,
                },
            )

        output = self._tracker.format_status()
        running = self._tracker.list_running()
        return ToolResult(
            title=f"Tasks: {len(running)} running, {len(self._tracker.list_all())} total",
            output=output,
            summary=f"{len(running)} running, {len(self._tracker.list_all())} total",
            metadata={"running": len(running), "total": len(self._tracker.list_all())},
        )
