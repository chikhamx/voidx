"""TodoWrite tool — stateful task list, Claude Code aligned.

Each call REPLACES the entire list. Status persists across calls via tracker.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult


class TodoItem(BaseModel):
    content: str = Field(description="Task description, one sentence.")
    status: str = Field(
        default="pending",
        description="pending | in_progress | completed | cancelled"
    )


class TodoInput(BaseModel):
    todos: list[TodoItem] = Field(
        description="Full todo list — replaces the previous list entirely. Include ALL items."
    )


class TodoWriteTool(BaseTool):
    id = "todo"
    description = (
        "Create and manage a task list. Each call REPLACES the entire list — "
        "pass the full updated list. Items:[{id, status, content}] "
        "Status: pending → in_progress → completed."
    )

    def __init__(self, tracker=None):
        super().__init__()
        self._tracker = tracker

    def parameters_schema(self) -> dict:
        return model_to_json_schema(TodoInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = TodoInput.model_validate(args)

        total = len(inp.todos)
        done = sum(1 for t in inp.todos if t.status == "completed")
        in_progress = sum(1 for t in inp.todos if t.status == "in_progress")
        pending = sum(1 for t in inp.todos if t.status == "pending")
        cancelled = sum(1 for t in inp.todos if t.status == "cancelled")

        # Store in tracker if available
        if self._tracker:
            self._tracker._todos = inp.todos

        # Format with icons and progress
        ICONS = {"pending": "○", "in_progress": "◐", "completed": "●", "cancelled": "✕"}
        lines = []

        # Progress bar
        if total > 0:
            pct = done / total
            bar_len = 20
            filled = int(bar_len * pct)
            bar = "█" * filled + "░" * (bar_len - filled)
            lines.append(f"[{bar}] {done}/{total} done")

        # Group by status
        for status in ["in_progress", "pending", "completed", "cancelled"]:
            items = [t for t in inp.todos if t.status == status]
            if not items:
                continue
            for item in items:
                lines.append(f"  {ICONS[item.status]} {item.content}")

        return ToolResult(
            title=f"Todo: {done}/{total} done · {in_progress} active · {pending} pending",
            output="\n".join(lines),
            metadata={
                "total": total, "done": done, "in_progress": in_progress,
                "pending": pending, "cancelled": cancelled,
            },
        )
