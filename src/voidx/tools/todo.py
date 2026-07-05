"""TodoWrite tool — stateful task list, Claude Code aligned.

Supports three operations:
- write: Full list replacement (each call REPLACES the entire list)
- update: Incremental update by semantic id
- read: Read-only query with filter
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from voidx.runtime.todo import TodoStatus
from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult


class TodoItem(BaseModel):
    id: str = Field(..., max_length=20, description="Semantic id for the todo item (e.g., 'schema', 'api').")
    content: str = Field(description="Task description, one sentence.")
    status: TodoStatus = Field(
        default="pending",
        description="pending | active | done"
    )


class TodoUpdateOp(BaseModel):
    id: str = Field(..., description="Target item id.")
    status: TodoStatus = Field(..., description="New status.")
    content: str | None = Field(default=None, description="Optional: update description too.")


TodoReadFilter = Literal["all", "pending", "active", "done"]


class TodoInput(BaseModel):
    op: Literal["write", "update", "read"] = Field(
        default="write",
        description="Operation: 'write' (full replace), 'update' (incremental by id), 'read' (query only)."
    )
    todos: list[TodoItem] | None = Field(
        default=None,
        description="Full todo list — required for 'write' operation."
    )
    updates: list[TodoUpdateOp] | None = Field(
        default=None,
        description="List of updates — required for 'update' operation."
    )
    filter: TodoReadFilter = Field(
        default="all",
        description="Filter for 'read' operation: all, pending, active, done."
    )


class TodoWriteTool(BaseTool):
    id = "todo"
    description = (
        "Create and manage a task list. Supports write (full replace), "
        "update (incremental by id), and read (query with filter)."
    )

    def __init__(self, tracker=None):
        super().__init__()
        self._tracker = tracker

    def parameters_schema(self) -> dict:
        return model_to_json_schema(TodoInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = TodoInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", summary="todo: invalid arguments", metadata={"error": True})

        if inp.op == "read":
            return self._execute_read(inp)
        elif inp.op == "update":
            return await self._execute_update(inp)
        else:  # write
            return await self._execute_write(inp)

    def _execute_read(self, inp: TodoInput) -> ToolResult:
        """Read-only operation with filter. No side effects."""
        if self._tracker is None:
            return ToolResult(
                title="Todo: No tracker available",
                output="Todo tracker is not available in this runtime.",
                summary="error: no tracker",
                metadata={"error": True, "reason": "no_tracker", "todo_op": "read"},
            )
        
        # Get current todos from tracker
        current_todos = self._tracker.get_todos()
        if not current_todos:
            return ToolResult(
                title="Todo: Empty",
                output="Todo list is empty.",
                summary="Todo list is empty.",
                metadata={"todo_op": "read"},
            )
        
        # Apply filter
        filtered_items = self._filter_items(current_todos, inp.filter)
        
        # Build summary
        total = len(current_todos)
        done = sum(1 for t in current_todos.values() if t["status"] == "done")
        active = sum(1 for t in current_todos.values() if t["status"] == "active")
        pending = sum(1 for t in current_todos.values() if t["status"] == "pending")
        
        summary = f"{done}/{total} done · {active} active · {pending} pending"
        
        # Format output
        ICONS = {"pending": "○", "active": "◐", "done": "●"}
        lines = []
        
        if filtered_items:
            lines.append(f"Todo {inp.filter} ({len(filtered_items)} items):")
            for item_id, item_data in filtered_items.items():
                lines.append(f"  {ICONS[item_data['status']]} {item_id}: {item_data['content']}")
        else:
            lines.append(f"No items match filter: {inp.filter}")
        
        return ToolResult(
            title=f"Todo {inp.filter}: {len(filtered_items)} items",
            output="\n".join(lines),
            summary=summary,
            metadata={
                "total": total, "done": done, "active": active,
                "pending": pending,
                "todo_summary": summary,
                "todo_op": "read",
            },
        )

    async def _execute_update(self, inp: TodoInput) -> ToolResult:
        """Incremental update by id. Skips unknown ids."""
        if inp.updates is None:
            return ToolResult(
                title="Todo: Error",
                output="'updates' is required for update operation.",
                summary="'updates' is required.",
                metadata={"error": True, "reason": "updates_required"},
            )
        
        if self._tracker is None:
            return ToolResult(
                title="Todo: No tracker available",
                output="Todo tracker is not available in this runtime.",
                summary="error: no tracker",
                metadata={"error": True, "reason": "no_tracker", "todo_op": "update"},
            )
        
        current_todos = self._tracker.get_todos()
        if not current_todos:
            return ToolResult(
                title="Todo: Empty",
                output="Todo list is empty.",
                summary="Todo list is empty.",
                metadata={"todo_op": "update"},
            )
        
        # Apply updates
        warnings = []
        updated_count = 0

        for update in inp.updates:
            if update.id in current_todos:
                current_todos[update.id]["status"] = update.status
                if update.content is not None:
                    current_todos[update.id]["content"] = update.content
                updated_count += 1
            else:
                warnings.append(f"Skipped unknown ids: {update.id}")

        # Update tracker
        self._tracker.set_todos_from_dict(current_todos)
        
        # Build response
        total = len(current_todos)
        done = sum(1 for t in current_todos.values() if t["status"] == "done")
        active = sum(1 for t in current_todos.values() if t["status"] == "active")
        pending = sum(1 for t in current_todos.values() if t["status"] == "pending")
        
        summary = f"{done}/{total} done · {active} active · {pending} pending"
        
        # Format output
        ICONS = {"pending": "○", "active": "◐", "done": "●"}
        lines = []
        
        if warnings:
            lines.append(f"Updated {updated_count} items. {', '.join(warnings)}")
        else:
            lines.append(f"Updated {updated_count} items.")
        
        for item_id, item_data in current_todos.items():
            lines.append(f"  {ICONS[item_data['status']]} {item_id}: {item_data['content']}")
        
        metadata = {
            "total": total, "done": done, "active": active,
            "pending": pending,
            "todo_items": [{"id": k, **v} for k, v in current_todos.items()],
            "todo_summary": summary,
        }
        
        if warnings:
            metadata["warnings"] = warnings
        
        return ToolResult(
            title=f"Todo: {done}/{total} done · {active} active · {pending} pending",
            output="\n".join(lines),
            summary=summary,
            metadata=metadata,
        )

    async def _execute_write(self, inp: TodoInput) -> ToolResult:
        """Full list replacement."""
        if inp.todos is None:
            return ToolResult(
                title="Todo: Error",
                output="'todos' is required for write operation.",
                summary="'todos' is required.",
                metadata={"error": True, "reason": "todos_required"},
            )
        
        # Check for duplicate ids
        seen_ids = set()
        duplicate_ids = []
        for item in inp.todos:
            if item.id in seen_ids:
                duplicate_ids.append(item.id)
            seen_ids.add(item.id)
        
        if duplicate_ids:
            return ToolResult(
                title="Todo: Error",
                output=f"Duplicate ids found: {', '.join(duplicate_ids)}",
                summary=f"Duplicate ids: {', '.join(duplicate_ids)}",
                metadata={"error": True, "reason": "duplicate_ids", "duplicate_ids": duplicate_ids},
            )
        
        # Convert to dict for storage
        todos_dict = {}
        for item in inp.todos:
            todos_dict[item.id] = {
                "content": item.content,
                "status": item.status,
            }
        
        # Store in tracker if available
        if self._tracker:
            self._tracker.set_todos_from_dict(todos_dict)
        
        # Build response
        total = len(inp.todos)
        done = sum(1 for t in inp.todos if t.status == "done")
        active = sum(1 for t in inp.todos if t.status == "active")
        pending = sum(1 for t in inp.todos if t.status == "pending")
        
        summary = f"{done}/{total} done · {active} active · {pending} pending"
        
        # Format output
        ICONS = {"pending": "○", "active": "◐", "done": "●"}
        lines = []
        lines.append(f"Written {total} items.")
        
        for item in inp.todos:
            lines.append(f"  {ICONS[item.status]} {item.id}: {item.content}")
        
        lines.append(f"Summary: {summary}")
        
        return ToolResult(
            title=f"Todo: {done}/{total} done · {active} active · {pending} pending",
            output="\n".join(lines),
            summary=summary,
            metadata={
                "total": total, "done": done, "active": active,
                "pending": pending,
                "todo_items": [{"id": item.id, "content": item.content, "status": item.status} for item in inp.todos],
                "todo_summary": summary,
            },
        )

    def _filter_items(self, todos: dict, filter_type: TodoReadFilter) -> dict:
        """Filter todo items by status."""
        if filter_type == "all":
            return todos
        return {k: v for k, v in todos.items() if v["status"] == filter_type}
