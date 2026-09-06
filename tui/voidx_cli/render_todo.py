"""Pinned todo rendering helpers."""

from __future__ import annotations

from dataclasses import replace

from rich.text import Text

from voidx.presentation.output.dock import dock
from voidx.presentation.output.dock.todo import (
    GLOBAL_TODO_RENDER_POLICY,
    TodoRenderLine,
    build_todo_render_plan,
)
from .helpers import _clip_cells


_TODO_PINNED_STYLES = {
    "pending": "#8F9BA8",
    "active": "#7AA2F7",
    "done": "#A3BE8C",
}
_TODO_PINNED_ICONS = {
    "pending": "○",
    "active": "◐",
    "done": "●",
}


class _TodoRendererMixin:
    def _pinned_todo_max_rows(self, render_height: int, bottom_fixed_lines: int) -> int:
        if dock.todo_state() is None:
            return 0
        available_rows = render_height - bottom_fixed_lines
        return max(1, min(1 + (GLOBAL_TODO_RENDER_POLICY.body_row_budget or 0), available_rows))

    def _render_pinned_todo_elements(
        self,
        width: int,
        *,
        max_rows: int | None = None,
    ) -> list[Text]:
        state = dock.todo_state()
        if state is None:
            return []

        row_limit = (
            1 + (GLOBAL_TODO_RENDER_POLICY.body_row_budget or 0)
            if max_rows is None
            else max_rows
        )
        if row_limit <= 0:
            return []

        elements = [
            Text(_clip_cells(f"Todo: {state.summary}", width), style="bold #A3BE8C")
        ]
        if row_limit <= 1:
            return elements

        policy = GLOBAL_TODO_RENDER_POLICY
        if policy.body_row_budget is not None:
            physical_body_budget = min(policy.body_row_budget, row_limit - 1)
            policy = replace(policy, body_row_budget=physical_body_budget)
        plan = build_todo_render_plan(state, policy)

        for line in plan.logical_lines:
            if len(elements) >= row_limit:
                break
            if line.kind == "empty":
                continue
            if line.kind == "ellipsis":
                elements.append(
                    Text(
                        _clip_cells(f"  … {line.omitted_count} more todos", width),
                        style="dim",
                    )
                )
                continue
            assert isinstance(line, TodoRenderLine)
            assert line.item is not None
            item = line.item
            icon = _TODO_PINNED_ICONS.get(item.status, "○")
            style = _TODO_PINNED_STYLES.get(item.status, "#8F9BA8")
            elements.append(
                Text(_clip_cells(f"  {icon} {item.content}", width), style=style)
            )
        return elements
