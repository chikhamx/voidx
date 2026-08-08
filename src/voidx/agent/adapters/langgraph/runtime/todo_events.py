"""Helpers for turning todo tool results into UI events."""

from __future__ import annotations

from voidx.agent.domain.ui_events import TodoItemPayload, TodoUpdated

from voidx.agent.application.todo_state import todo_run_state_from_result


def todo_updated_event(result, *, agent_id: int = -1):
    state = todo_run_state_from_result(result)
    if state is None:
        return None
    
    # Get todo_op from metadata
    metadata = getattr(result, "metadata", {}) or {}
    todo_op = metadata.get("todo_op", "write")
    
    return TodoUpdated(
        agent_id=agent_id,
        items=[TodoItemPayload(id=item.id, content=item.content, status=item.status) for item in state.items],
        summary=state.summary,
        todo_op=todo_op,
    )
