from voidx.presentation.output.dock.todo import (
    DockTodoItem,
    DockTodoState,
    TodoRenderPolicy,
    build_todo_render_plan,
    render_todo_plan_lines,
)


def _state(*items: tuple[str, str, str]) -> DockTodoState:
    return DockTodoState(
        summary="summary",
        items=tuple(DockTodoItem(item_id, content, status) for item_id, content, status in items),
    )


def test_global_policy_reserves_ellipsis_inside_four_row_body_budget():
    plan = build_todo_render_plan(
        _state(*[(str(index), f"task {index}", "pending") for index in range(6)]),
        TodoRenderPolicy(
            max_visible_items=None,
            body_row_budget=4,
            ellipsis_consumes_budget=True,
            include_item_id=False,
        ),
    )

    assert [item.content for item in plan.visible_items] == ["task 0", "task 1", "task 2"]
    assert plan.omitted_count == 3
    assert plan.has_ellipsis is True
    assert [line.kind for line in plan.logical_lines] == ["item", "item", "item", "ellipsis"]


def test_subagent_policy_allows_eight_items_and_an_extra_ellipsis_row():
    plan = build_todo_render_plan(
        _state(*[(str(index), f"task {index}", "pending") for index in range(10)]),
        TodoRenderPolicy(
            max_visible_items=8,
            body_row_budget=None,
            ellipsis_consumes_budget=False,
            include_item_id=True,
        ),
    )

    assert len(plan.visible_items) == 8
    assert plan.omitted_count == 2
    assert len(plan.logical_lines) == 9
    assert plan.logical_lines[-1].omitted_count == 2
    assert plan.payload_signature == tuple((item.id, item.content, item.status) for item in plan.ordered_items)


def test_todo_plan_stably_orders_known_statuses_and_preserves_unknown_items():
    plan = build_todo_render_plan(
        _state(
            ("done", "done", "done"),
            ("unknown-1", "unknown 1", "blocked"),
            ("pending", "pending", "pending"),
            ("active", "active", "active"),
            ("unknown-2", "unknown 2", "paused"),
        ),
        TodoRenderPolicy(max_visible_items=None, body_row_budget=None),
    )

    assert [item.id for item in plan.ordered_items] == [
        "active",
        "pending",
        "done",
        "unknown-1",
        "unknown-2",
    ]
    assert [line.item.status for line in plan.logical_lines if line.item] == [
        "active",
        "pending",
        "done",
        "blocked",
        "paused",
    ]


def test_render_todo_plan_lines_uses_cell_width_and_prefix_width():
    plan = build_todo_render_plan(
        _state(("id", "宽度很长的任务", "pending")),
        TodoRenderPolicy(
            max_visible_items=8,
            body_row_budget=None,
            ellipsis_consumes_budget=False,
            include_item_id=True,
        ),
    )

    lines = render_todo_plan_lines(plan, width=12, prefix="│  ")

    assert len(lines) == 1
    assert lines[0].startswith("│  ")
    assert lines[0].endswith("…")
    assert len(lines[0].encode("utf-8")) > 12
    assert "宽度很长的任务" not in lines[0]


def test_empty_todo_plan_has_a_single_empty_line():
    plan = build_todo_render_plan(
        DockTodoState(summary="0/0", items=()),
        TodoRenderPolicy(max_visible_items=8, body_row_budget=None),
    )

    assert plan.visible_items == ()
    assert plan.omitted_count == 0
    assert plan.logical_lines[0].kind == "empty"
    assert render_todo_plan_lines(plan, width=20) == ["No todos"]
