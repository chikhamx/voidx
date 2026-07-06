"""Pinned todo rendering helpers."""

from __future__ import annotations

from rich.text import Text

from voidx.ui.output.dock import dock
from voidx_tui.helpers import _clip_cells


_TODO_PINNED_MAX_ITEMS = 4
_TODO_PINNED_ORDER = ("active", "pending", "done")
_TODO_PINNED_ICONS = {
    "pending": "○",
    "active": "◐",
    "done": "●",
}
_TODO_PINNED_STYLES = {
    "pending": "#8F9BA8",
    "active": "#7AA2F7",
    "done": "#A3BE8C",
}


class _TodoRendererMixin:
    def _pinned_todo_max_rows(self, render_height: int, bottom_fixed_lines: int) -> int:
        if dock.todo_state() is None:
            return 0
        available_rows = render_height - bottom_fixed_lines
        row_budget = 1 + _TODO_PINNED_MAX_ITEMS
        return max(1, min(row_budget, available_rows))

    def _pinned_todo_row_count(self, width: int, max_rows: int | None = None) -> int:
        return len(self._render_pinned_todo_elements(width, max_rows=max_rows))

    def _render_pinned_todo_elements(
        self,
        width: int,
        *,
        max_rows: int | None = None,
    ) -> list[Text]:
        state = dock.todo_state()
        if state is None:
            return []

        row_limit = 1 + _TODO_PINNED_MAX_ITEMS if max_rows is None else max_rows
        if row_limit <= 0:
            return []
        elements = [
            Text(_clip_cells(f"Todo: {state.summary}", width), style="bold #A3BE8C")
        ]
        if row_limit <= 1 or not state.items:
            return elements[:row_limit]

        ordered_items = [
            item
            for status in _TODO_PINNED_ORDER
            for item in state.items
            if item.status == status
        ]
        ordered_items.extend(
            item for item in state.items if item.status not in _TODO_PINNED_ORDER
        )

        available_item_rows = row_limit - 1
        visible_count = available_item_rows
        if len(ordered_items) > available_item_rows:
            visible_count = max(available_item_rows - 1, 0)
        for item in ordered_items[:visible_count]:
            icon = _TODO_PINNED_ICONS.get(item.status, "○")
            style = _TODO_PINNED_STYLES.get(item.status, "#8F9BA8")
            elements.append(Text(_clip_cells(f"  {icon} {item.content}", width), style=style))

        omitted = len(ordered_items) - visible_count
        if omitted > 0 and len(elements) < row_limit:
            elements.append(
                Text(_clip_cells(f"  … {omitted} more todos", width), style="dim")
            )
        return elements[:row_limit]
