"""Helpers for turning todo tool results into UI events."""

from __future__ import annotations

from voidx.agent.todo_state import todo_run_state_from_result
from voidx.runtime.ui import TodoItemPayload, TodoUpdated


def todo_updated_event(result, *, agent_id: int = -1):
    state = todo_run_state_from_result(result)
    if state is None:
        return None
    return TodoUpdated(
        agent_id=agent_id,
        items=[TodoItemPayload(content=item.content, status=item.status) for item in state.items],
        summary=state.summary,
    )
