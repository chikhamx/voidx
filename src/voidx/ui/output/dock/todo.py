"""Pinned TODO state shared by dock renderers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from rich.markup import escape


# These TODO_* names refer to the todo tool/render feature, not pending code work.
TODO_MAX_VISIBLE_ITEMS = 8
TODO_STATUS_ORDER = ("in_progress", "pending", "completed", "cancelled")
TODO_ICONS = {
    "pending": "[dim]○[/dim]",
    "in_progress": "[#7AA2F7]◐[/#7AA2F7]",
    "completed": "[#A3BE8C]●[/#A3BE8C]",
    "cancelled": "[#BF616A]✕[/#BF616A]",
}
TODO_HEADER_STYLE = "#A3BE8C"
TODO_MUTED_STYLE = "#8F9BA8"


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


def todo_state_payload(state: DockTodoState) -> dict[str, Any]:
    return {
        "summary": state.summary,
        "items": [
            {"content": item.content, "status": item.status}
            for item in state.items
        ],
    }


def render_todo_header(state: DockTodoState) -> str:
    return (
        f"[bold {TODO_HEADER_STYLE}]Todo[/]: "
        f"[{TODO_MUTED_STYLE}]{escape(state.summary)}[/]"
    )


def render_todo_state_lines(state: DockTodoState) -> list[str]:
    total = len(state.items)
    if total == 0:
        return ["[dim]No todos[/dim]"]

    lines: list[str] = []

    ordered_items = [
        item
        for status in TODO_STATUS_ORDER
        for item in state.items
        if item.status == status
    ]
    visible_items = ordered_items[:TODO_MAX_VISIBLE_ITEMS]
    for item in visible_items:
        lines.append(f"  {TODO_ICONS[item.status]} {escape(item.content)}")
    omitted = len(ordered_items) - len(visible_items)
    if omitted > 0:
        lines.append(f"  [dim]… {omitted} more todos[/dim]")
    return lines


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
