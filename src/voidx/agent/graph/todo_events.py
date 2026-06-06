"""Helpers for turning todo tool results into UI events."""

from __future__ import annotations

from voidx.runtime.ui import TodoItemPayload, TodoUpdated


def todo_updated_event(result, *, agent_id: int = -1):
    metadata = getattr(result, "metadata", {}) or {}
    raw_items = metadata.get("todo_items")
    summary = metadata.get("todo_summary")
    if not isinstance(raw_items, list) or not isinstance(summary, str):
        return None
    try:
        return TodoUpdated(
            agent_id=agent_id,
            items=[TodoItemPayload.model_validate(item) for item in raw_items],
            summary=summary,
        )
    except Exception:
        return None
