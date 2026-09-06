"""Pinned TODO state shared by dock renderers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from rich.cells import cell_len
from rich.markup import escape


# These TODO_* names refer to the todo tool/render feature, not pending code work.
TODO_MAX_VISIBLE_ITEMS = 8
TODO_STATUS_ORDER = ("active", "pending", "done")
TODO_ICONS = {
    "pending": "[dim]○[/dim]",
    "active": "[#7AA2F7]◐[/#7AA2F7]",
    "done": "[#A3BE8C]●[/#A3BE8C]",
}
TODO_PLAIN_ICONS = {
    "pending": "○",
    "active": "◐",
    "done": "●",
}
TODO_HEADER_STYLE = "#A3BE8C"
TODO_MUTED_STYLE = "#8F9BA8"


@dataclass(frozen=True)
class TodoRenderPolicy:
    max_visible_items: int | None = None
    body_row_budget: int | None = None
    ellipsis_consumes_budget: bool = False
    include_item_id: bool = True

    def __post_init__(self) -> None:
        if self.max_visible_items is not None and self.max_visible_items < 0:
            raise ValueError("max_visible_items must not be negative")
        if self.body_row_budget is not None and self.body_row_budget < 0:
            raise ValueError("body_row_budget must not be negative")


@dataclass(frozen=True)
class DockTodoItem:
    id: str
    content: str
    status: str


@dataclass(frozen=True)
class DockTodoState:
    summary: str
    items: tuple[DockTodoItem, ...]


@dataclass(frozen=True)
class TodoRenderLine:
    kind: str
    item: DockTodoItem | None = None
    omitted_count: int = 0


@dataclass(frozen=True)
class TodoRenderPlan:
    summary: str
    ordered_items: tuple[DockTodoItem, ...]
    visible_items: tuple[DockTodoItem, ...]
    omitted_count: int
    has_ellipsis: bool
    logical_lines: tuple[TodoRenderLine, ...]
    payload_signature: tuple[tuple[str, str, str], ...]
    include_item_id: bool = True


GLOBAL_TODO_RENDER_POLICY = TodoRenderPolicy(
    body_row_budget=4,
    ellipsis_consumes_budget=True,
    include_item_id=False,
)
SUBAGENT_TODO_RENDER_POLICY = TodoRenderPolicy(
    max_visible_items=TODO_MAX_VISIBLE_ITEMS,
    ellipsis_consumes_budget=False,
    include_item_id=True,
)


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
            {"id": item.id, "content": item.content, "status": item.status}
            for item in state.items
        ],
    }


def build_todo_render_plan(
    state: DockTodoState,
    policy: TodoRenderPolicy,
) -> TodoRenderPlan:
    known = tuple(
        item
        for status in TODO_STATUS_ORDER
        for item in state.items
        if item.status == status
    )
    unknown = tuple(item for item in state.items if item.status not in TODO_STATUS_ORDER)
    ordered_items = known + unknown

    visible_cap = len(ordered_items)
    if policy.max_visible_items is not None:
        visible_cap = min(visible_cap, policy.max_visible_items)
    if policy.body_row_budget is not None:
        visible_cap = min(visible_cap, policy.body_row_budget)

    omitted_count = len(ordered_items) - visible_cap
    if omitted_count and policy.body_row_budget is not None and policy.ellipsis_consumes_budget:
        visible_cap = min(visible_cap, max(policy.body_row_budget - 1, 0))
        omitted_count = len(ordered_items) - visible_cap

    visible_items = ordered_items[:visible_cap]
    has_ellipsis = omitted_count > 0 and (
        policy.body_row_budget is None or policy.body_row_budget > 0
    )
    logical_lines = tuple(
        [TodoRenderLine(kind="item", item=item) for item in visible_items]
        + ([TodoRenderLine(kind="ellipsis", omitted_count=omitted_count)] if has_ellipsis else [])
    )
    if not logical_lines and not ordered_items:
        logical_lines = (TodoRenderLine(kind="empty"),)

    return TodoRenderPlan(
        summary=state.summary,
        ordered_items=ordered_items,
        visible_items=visible_items,
        omitted_count=omitted_count,
        has_ellipsis=has_ellipsis,
        logical_lines=logical_lines,
        payload_signature=tuple(
            (item.id, item.content, item.status) for item in ordered_items
        ),
        include_item_id=policy.include_item_id,
    )


def _clip_cells(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    if width == 1:
        return "…"
    limit = width - cell_len("…")
    result: list[str] = []
    used = 0
    for char in text:
        char_width = cell_len(char)
        if used + char_width > limit:
            break
        result.append(char)
        used += char_width
    return "".join(result) + "…"


def render_todo_plan_lines(
    plan: TodoRenderPlan,
    *,
    width: int,
    prefix: str = "",
) -> list[str]:
    if width <= 0:
        return []
    prefix = _clip_cells(prefix, width)
    content_width = max(width - cell_len(prefix), 0)
    lines: list[str] = []
    for line in plan.logical_lines:
        if line.kind == "empty":
            content = "No todos"
        elif line.kind == "ellipsis":
            content = f"  … {line.omitted_count} more todos"
        else:
            assert line.item is not None
            item = line.item
            icon = TODO_PLAIN_ICONS.get(item.status, TODO_PLAIN_ICONS["pending"])
            label = item.content
            if plan.include_item_id and item.id:
                label = f"{item.id}: {label}"
            content = f"  {icon} {label}"
        lines.append(prefix + _clip_cells(content, content_width))
    return lines


def render_todo_header(state: DockTodoState) -> str:
    return (
        f"[bold {TODO_HEADER_STYLE}]Todo[/]: "
        f"[{TODO_MUTED_STYLE}]{escape(state.summary)}[/]"
    )


def render_todo_state_lines(state: DockTodoState) -> list[str]:
    plan = build_todo_render_plan(state, SUBAGENT_TODO_RENDER_POLICY)
    lines: list[str] = []
    for line in plan.logical_lines:
        if line.kind == "empty":
            lines.append("[dim]No todos[/dim]")
            continue
        if line.kind == "ellipsis":
            lines.append(f"  [dim]… {line.omitted_count} more todos[/dim]")
            continue
        assert line.item is not None
        item = line.item
        icon = TODO_ICONS.get(item.status, TODO_ICONS["pending"])
        if item.id:
            lines.append(f"  {icon} {escape(item.id)}: {escape(item.content)}")
        else:
            lines.append(f"  {icon} {escape(item.content)}")
    return lines


def _todo_item_from_value(value: Any) -> DockTodoItem:
    if isinstance(value, DockTodoItem):
        return value
    if isinstance(value, dict):
        item_id = value.get("id", "")
        content = value.get("content")
        status = value.get("status")
    elif hasattr(value, "content") and hasattr(value, "status"):
        item_id = getattr(value, "id", "")
        content = getattr(value, "content")
        status = getattr(value, "status")
    else:
        raise TypeError("TODO item must be a dict or object with content/status")
    if content is None or status is None:
        raise ValueError("TODO item requires content and status")
    status_value = getattr(status, "value", status)
    return DockTodoItem(id=str(item_id), content=str(content), status=str(status_value))
