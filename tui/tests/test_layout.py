import pytest

from voidx_cli.layout import (
    BottomGeometry,
    LayoutSnapshot,
    RegionGeometry,
    SourceSlice,
    diff_layout,
    map_source_cursor,
    normalize_rendered_rows,
    project_bottom_viewport,
    LogicalRenderPlan,
    project_physical_viewport,
)


def _region(
    key: str,
    start: int,
    rows: tuple[str, ...],
    *,
    width: int = 20,
    patch_safe: bool = True,
) -> RegionGeometry:
    rendered = normalize_rendered_rows("\n".join(rows), width=width)
    return RegionGeometry(
        key=key,
        start_row=start,
        visual_rows=len(rows),
        width=width,
        content_signature=rendered.signature,
        patch_safe=patch_safe,
    )


def _snapshot(
    regions: tuple[RegionGeometry, ...],
    *,
    cursor_row: int = 5,
    cursor_col: int = 1,
    generation: int = 1,
    epoch: int = 1,
) -> LayoutSnapshot:
    empty = _region("empty", 1, ())
    bottom = BottomGeometry(
        rendered=normalize_rendered_rows("bottom", width=20),
        region=_region("bottom", 4, ("bottom",)),
        top_separator=empty,
        input=empty,
        middle_separator=empty,
        panel=empty,
        panel_status_separator=empty,
        status=_region("status", 4, ("bottom",)),
        cursor_row=cursor_row,
        cursor_col=cursor_col,
    )
    return LayoutSnapshot(
        terminal_width=20,
        terminal_height=10,
        frame_start_row=1,
        frame_rows=5,
        regions=regions,
        source_slices=(),
        bottom=bottom,
        cursor_row=cursor_row,
        cursor_col=cursor_col,
        scroll_epoch=epoch,
        generation=generation,
    )


def test_normalize_rendered_rows_preserves_meaningful_trailing_blank_row():
    rendered = normalize_rendered_rows("line\n", width=20)

    assert rendered.rows == ("line", "")
    assert rendered.visual_rows == 2
    assert rendered.signature
    assert rendered.patch_safe is True


def test_normalize_rendered_rows_distinguishes_empty_and_unsafe_controls():
    assert normalize_rendered_rows("", width=20).rows == ()
    assert normalize_rendered_rows("\x1b[31mred\x1b[0m", width=20).patch_safe is True
    assert normalize_rendered_rows("\x1b[2J", width=20).patch_safe is False
    assert normalize_rendered_rows("a\rb", width=20).patch_safe is False


def test_source_slice_maps_cursor_from_source_to_absolute_bottom_row():
    source = SourceSlice(
        key="bottom.input",
        source_start=4,
        source_end=8,
        projected_start=1,
        projected_end=5,
        mode="full",
    )

    assert map_source_cursor(source, source_row=6, bottom_start_row=10) == 13


def test_layout_snapshot_rejects_frame_or_cursor_outside_terminal():
    with pytest.raises(ValueError):
        _snapshot((_region("transcript", 1, ("a",)),), cursor_row=11)

    with pytest.raises(ValueError):
        LayoutSnapshot(
            terminal_width=20,
            terminal_height=4,
            frame_start_row=2,
            frame_rows=4,
            regions=(),
            source_slices=(),
            bottom=_snapshot((_region("transcript", 1, ("a",)),)).bottom,
            cursor_row=2,
            cursor_col=1,
            scroll_epoch=1,
            generation=1,
        )


def test_layout_diff_reports_region_patch_suffix_cursor_and_full_cases():
    old = _snapshot((_region("transcript", 1, ("old",)),), cursor_row=5, generation=1)
    same = _snapshot((_region("transcript", 1, ("old",)),), cursor_row=5, generation=2)
    changed = _snapshot((_region("transcript", 1, ("new",)),), cursor_row=5, generation=3)
    grown = _snapshot((_region("transcript", 1, ("new", "extra")),), cursor_row=5, generation=4)
    moved_cursor = _snapshot((_region("transcript", 1, ("old",)),), cursor_row=4, generation=5)
    unsafe = _snapshot(
        (_region("transcript", 1, ("old",), patch_safe=False),),
        generation=6,
    )

    assert diff_layout(old, same).kind == "unchanged"
    assert diff_layout(old, changed).kind == "regions"
    assert diff_layout(old, grown).kind == "suffix"
    assert diff_layout(old, moved_cursor).kind == "cursor"
    assert diff_layout(old, unsafe).kind == "full"
    assert diff_layout(None, old).kind == "full"


def test_layout_diff_does_not_compare_generation():
    old = _snapshot((_region("transcript", 1, ("same",)),), generation=1)
    new = _snapshot((_region("transcript", 1, ("same",)),), generation=99)

    assert diff_layout(old, new).kind == "unchanged"



def _rendered(*rows: str):
    return normalize_rendered_rows("\n".join(rows), width=20)


def test_bottom_viewport_preserves_child_order_and_maps_cursor_with_all_separators():
    source_children = (
        ("top_separator", _rendered("top")),
        ("input", _rendered("input 0", "input 1", "input 2")),
        ("middle_separator", _rendered("middle")),
        ("panel", _rendered("panel")),
        ("panel_status_separator", _rendered("panel-status")),
        ("status", _rendered("status")),
    )

    plan = project_bottom_viewport(
        source_children,
        terminal_height=10,
        source_cursor_key="input",
        source_cursor_row=2,
        cursor_col=4,
        start_row=5,
        width=20,
    )

    assert [key for key, _ in plan.source_children] == [
        "top_separator",
        "input",
        "middle_separator",
        "panel",
        "panel_status_separator",
        "status",
    ]
    assert plan.projected.rendered.rows == (
        "top",
        "input 0",
        "input 1",
        "input 2",
        "middle",
        "panel",
        "panel-status",
        "status",
    )
    assert plan.projected_cursor_row == 3
    assert plan.projected.cursor_row == 8
    assert plan.projected.cursor_col == 4
    assert [slice_.key for slice_ in plan.projected_slices] == [
        "bottom.top_separator",
        "bottom.input",
        "bottom.middle_separator",
        "bottom.panel",
        "bottom.panel_status_separator",
        "bottom.status",
    ]


def test_bottom_viewport_overflow_keeps_cursor_window_and_never_orphan_separators():
    source_children = (
        ("top_separator", _rendered("top")),
        ("input", _rendered("input 0", "input 1", "input 2", "input 3", "input 4")),
        ("middle_separator", _rendered("middle")),
        ("panel", _rendered("panel 0", "panel 1", "panel 2")),
        ("panel_status_separator", _rendered("panel-status")),
        ("status", _rendered("status 0", "status 1")),
    )

    plan = project_bottom_viewport(
        source_children,
        terminal_height=4,
        source_cursor_key="input",
        source_cursor_row=3,
        start_row=6,
        width=20,
    )

    assert plan.projected.rendered.rows == (
        "input 1",
        "input 2",
        "input 3",
        "input 4",
    )
    assert plan.projected.rendered.visual_rows == 4
    assert plan.omitted_input_before == 1
    assert plan.omitted_input_after == 0
    cursor_slice = next(
        slice_ for slice_ in plan.projected_slices if slice_.key == "bottom.input"
    )
    assert cursor_slice.source_start <= 3 < cursor_slice.source_end
    assert plan.projected_cursor_row == 2
    assert plan.projected.cursor_row == 8
    assert plan.projected.panel.visual_rows == 0
    assert plan.projected.middle_separator.visual_rows == 0


def test_bottom_viewport_maps_cursor_when_window_starts_after_multiple_source_rows():
    source_children = (("input", _rendered(*[f"input {index}" for index in range(12)])),)

    plan = project_bottom_viewport(
        source_children,
        terminal_height=4,
        source_cursor_key="input",
        source_cursor_row=10,
        start_row=3,
        width=20,
    )

    assert plan.projected_slices[0].source_start == 8
    assert plan.projected_cursor_row == 2
    assert plan.projected.cursor_row == 5



def test_bottom_viewport_drops_separator_when_an_adjacent_content_child_is_absent():
    source_children = (
        ("top_separator", _rendered("top")),
        ("input", _rendered("input")),
        ("middle_separator", _rendered("middle")),
        ("panel", _rendered()),
        ("panel_status_separator", _rendered("panel-status")),
        ("status", _rendered("status")),
    )

    plan = project_bottom_viewport(
        source_children,
        terminal_height=4,
        source_cursor_key="input",
        source_cursor_row=0,
        start_row=1,
        width=20,
    )

    assert [slice_.key for slice_ in plan.projected_slices] == [
        "bottom.top_separator",
        "bottom.input",
        "bottom.status",
    ]
    assert plan.projected.middle_separator.visual_rows == 0
    assert plan.projected.panel_status_separator.visual_rows == 0



def _logical_plan_for_viewport(
    *,
    bottom_terminal_height: int = 12,
    bottom_start_row: int = 1,
) -> LogicalRenderPlan:
    bottom = project_bottom_viewport(
        (
            ("input", _rendered("input 0", "input 1")),
            ("status", _rendered("status")),
        ),
        terminal_height=bottom_terminal_height,
        source_cursor_key="input",
        source_cursor_row=1,
        start_row=bottom_start_row,
        width=20,
    )
    return LogicalRenderPlan(
        source_regions=(
            _rendered("transcript 0", "transcript 1", "transcript 2", "transcript 3", "transcript 4", "transcript 5"),
            _rendered("vibe"),
            _rendered("thinking 0", "thinking 1"),
            _rendered("todo 0", "todo 1"),
        ),
        bottom_source=bottom,
        source_cursor=("input", 1),
        source_signature=(("state", 1),),
    )


def test_physical_viewport_keeps_region_order_and_takes_transcript_tail_last():
    logical = _logical_plan_for_viewport()

    physical = project_physical_viewport(
        logical,
        terminal_width=20,
        terminal_height=9,
    )

    assert [region.rows for region in physical.projected_regions] == [
        ("transcript 5",),
        ("vibe",),
        ("thinking 0", "thinking 1"),
        ("todo 0", "todo 1"),
    ]
    assert [slice_.key for slice_ in physical.source_slices] == [
        "transcript",
        "vibe",
        "thinking",
        "todo",
        "bottom.input",
        "bottom.status",
    ]
    transcript_slice = physical.source_slices[0]
    assert transcript_slice.source_start == 5
    assert transcript_slice.source_end == 6
    assert transcript_slice.mode == "tail"
    assert physical.bottom.region.start_row == 7
    assert physical.bottom.cursor_row == 8
    assert physical.cursor_row == 8
    assert physical.frame_rows == 9
    assert physical.source_signature == logical.source_signature


def test_physical_viewport_reprojects_bottom_for_late_frame_start():
    logical = _logical_plan_for_viewport(bottom_terminal_height=12)
    physical = project_physical_viewport(
        logical,
        terminal_width=20,
        terminal_height=6,
        frame_start_row=3,
    )

    assert physical.projected_regions == (
        normalize_rendered_rows("", width=20),
        normalize_rendered_rows("", width=20),
        normalize_rendered_rows("", width=20),
        normalize_rendered_rows("todo 1", width=20),
    )
    assert physical.bottom.region.start_row == 4
    assert physical.bottom.rendered.visual_rows == 3
    assert physical.frame_rows == 4
    assert 3 <= physical.cursor_row <= 6
    assert physical.cursor_row == 5
    assert physical.cursor_row == physical.bottom.cursor_row



def test_physical_viewport_rejects_frame_start_outside_terminal():
    logical = _logical_plan_for_viewport()

    with pytest.raises(ValueError):
        project_physical_viewport(
            logical,
            terminal_width=20,
            terminal_height=6,
            frame_start_row=7,
        )


def test_physical_viewport_anchor_bottom_pins_bottom_to_terminal_bottom():
    logical = _logical_plan_for_viewport(bottom_terminal_height=12)
    physical = project_physical_viewport(
        logical,
        terminal_width=20,
        terminal_height=10,
        frame_start_row=1,
        anchor_bottom=True,
    )

    assert physical.frame_start_row == 1
    assert physical.bottom.region.start_row == 8
    assert physical.bottom.rendered.visual_rows == 3


def test_physical_viewport_anchor_bottom_unfilled_terminal_aligns_frame_start():
    bottom = project_bottom_viewport(
        (
            ("input", _rendered("input 0", "input 1")),
            ("status", _rendered("status")),
        ),
        terminal_height=10,
        source_cursor_key="input",
        source_cursor_row=1,
        start_row=1,
        width=20,
    )
    logical = LogicalRenderPlan(
        source_regions=(
            _rendered("transcript 0", "transcript 1"),
            _rendered(),
            _rendered(),
            _rendered(),
        ),
        bottom_source=bottom,
        source_cursor=("input", 1),
        source_signature=(("state", 1),),
    )
    physical = project_physical_viewport(
        logical,
        terminal_width=20,
        terminal_height=10,
        frame_start_row=1,
        anchor_bottom=True,
    )
    assert physical.frame_start_row == 6
    assert physical.bottom.region.start_row == 8
    assert physical.cursor_row == 9
