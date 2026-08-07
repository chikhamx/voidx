"""Todo runtime state helpers and replay sanitization."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from voidx.agent.domain.task.todo import TodoRunItem, TodoRunState
from voidx.agent.domain.task.todo import TodoStatus


def todo_run_state_from_result(result: object) -> TodoRunState | None:
    metadata = getattr(result, "metadata", {}) or {}
    
    # Short-circuit for read operations
    todo_op = metadata.get("todo_op")
    if todo_op == "read":
        return None
    
    raw_items = metadata.get("todo_items")
    summary = metadata.get("todo_summary")
    if not isinstance(raw_items, list) or not isinstance(summary, str):
        return None
    try:
        items = [TodoRunItem.model_validate(item) for item in raw_items]
    except Exception:
        return None
    
    # Build counts
    total = len(items)
    done = sum(1 for item in items if item.status == TodoStatus.DONE)
    active = sum(1 for item in items if item.status == TodoStatus.ACTIVE)
    pending = sum(1 for item in items if item.status == TodoStatus.PENDING)
    
    # Build active_items (only active)
    active_items = [item for item in items if item.status == TodoStatus.ACTIVE]
    
    return TodoRunState(
        summary=summary,
        total=total,
        done=done,
        active=active,
        pending=pending,
        active_items=active_items,
        items=items,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def apply_todo_state_to_host(host: object, raw_state: object) -> None:
    task_state = getattr(host, "_task_state", None)
    tracker = getattr(host, "_tracker", None)

    if raw_state is None:
        if task_state is not None:
            task_state.todo_state = None
        if tracker is not None:
            tracker.clear_todos()
        return

    try:
        todo_state = raw_state if isinstance(raw_state, TodoRunState) else TodoRunState.model_validate(raw_state)
    except (TypeError, ValueError):
        return

    if task_state is not None:
        task_state.todo_state = todo_state
    if tracker is not None:
        if todo_state.total > 0:
            # Restore full todo list (all statuses) into tracker
            todos_dict = {}
            for item in todo_state.items:
                todos_dict[item.id] = {"content": item.content, "status": item.status}
            tracker.set_todos_from_dict(todos_dict)
        else:
            tracker.clear_todos()
