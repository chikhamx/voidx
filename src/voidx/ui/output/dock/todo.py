"""Pinned TODO state shared by dock renderers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class DockTodoItem:
    content: str
    status: str


@dataclass(frozen=True)
class DockTodoState:
    summary: str
    items: tuple[DockTodoItem, ...]


def todo_state_from_items(summary: str, items: Sequence[Any]) -> DockTodoState:
    return DockTodoState(
        summary=str(summary),
        items=tuple(_todo_item_from_value(item) for item in items),
    )


def todo_state_from_payload(payload: dict[str, Any]) -> DockTodoState | None:
    summary = payload.get("summary")
    items = payload.get("items")
    if not isinstance(summary, str) or not isinstance(items, list):
        return None
    try:
        return todo_state_from_items(summary, items)
    except (AttributeError, TypeError, ValueError):
        return None


def _todo_item_from_value(value: Any) -> DockTodoItem:
    if isinstance(value, DockTodoItem):
        return value
    if isinstance(value, dict):
        content = value.get("content")
        status = value.get("status")
    elif hasattr(value, "content") and hasattr(value, "status"):
        content = getattr(value, "content")
        status = getattr(value, "status")
    else:
        raise TypeError("TODO item must be a dict or object with content/status")
    if content is None or status is None:
        raise ValueError("TODO item requires content and status")
    return DockTodoItem(content=str(content), status=str(status))
