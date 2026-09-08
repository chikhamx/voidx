"""Pure terminal layout models and deterministic viewport diff helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Literal, Sequence


_PATCH_UNSAFE_CSI_FINALS = frozenset("@ABCD EF GHIJKLMPSTXZcdfghilmnqrsu".replace(" ", ""))
_CSI_FINAL_RE = re.compile(r"\x1b\[([0-?]*[ -/]*)([@-~])")
_OSC_RE = re.compile(r"\x1b\]8;[^\x07]*(?:\x07|\x1b\\)")


@dataclass(frozen=True)
class RenderedRows:
    ansi: str
    rows: tuple[str, ...]
    visual_rows: int
    signature: tuple[Any, ...]
    patch_safe: bool


@dataclass(frozen=True)
class RegionGeometry:
    key: str
    start_row: int
    visual_rows: int
    width: int
    content_signature: tuple[Any, ...]
    patch_safe: bool = True

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("region key must not be empty")
        if self.start_row < 1:
            raise ValueError("region start_row must be positive")
        if self.visual_rows < 0:
            raise ValueError("region visual_rows must not be negative")
        if self.width < 1:
            raise ValueError("region width must be positive")


@dataclass(frozen=True)
class SourceSlice:
    key: str
    source_start: int
    source_end: int
    projected_start: int
    projected_end: int
    mode: Literal["full", "compact", "tail"] = "full"

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("source slice key must not be empty")
        if self.source_start < 0 or self.source_end < self.source_start:
            raise ValueError("invalid source slice range")
        if self.projected_start < 0 or self.projected_end < self.projected_start:
            raise ValueError("invalid projected slice range")
        if self.mode not in {"full", "compact", "tail"}:
            raise ValueError("invalid source slice mode")


@dataclass(frozen=True)
class BottomGeometry:
    rendered: RenderedRows
    region: RegionGeometry
    top_separator: RegionGeometry
    input: RegionGeometry
    middle_separator: RegionGeometry
    panel: RegionGeometry
    panel_status_separator: RegionGeometry
    status: RegionGeometry
    cursor_row: int
    cursor_col: int

    def __post_init__(self) -> None:
        if self.cursor_row < 1 or self.cursor_col < 1:
            raise ValueError("bottom cursor coordinates must be positive")
        if self.region.visual_rows != self.rendered.visual_rows:
            raise ValueError("bottom region and rendered rows disagree")


@dataclass(frozen=True)
class BottomViewportPlan:
    source_signature: tuple[Any, ...]
    source_children: tuple[tuple[str, RenderedRows], ...]
    projected: BottomGeometry
    projected_slices: tuple[SourceSlice, ...]
    source_cursor_key: str
    source_cursor_row: int
    projected_cursor_row: int
    omitted_input_before: int = 0
    omitted_input_after: int = 0
    panel_omitted: bool = False
    status_omitted_rows: int = 0


@dataclass(frozen=True)
class LogicalRenderPlan:
    source_regions: tuple[RenderedRows, ...]
    bottom_source: BottomViewportPlan
    source_cursor: tuple[str, int]
    source_signature: tuple[Any, ...]


@dataclass(frozen=True)
class PhysicalViewportPlan:
    projected_regions: tuple[RenderedRows, ...]
    bottom: BottomGeometry
    source_slices: tuple[SourceSlice, ...]
    frame_rows: int
    cursor_row: int
    cursor_col: int
    source_signature: tuple[Any, ...]

    def __post_init__(self) -> None:
        if self.frame_rows < 0:
            raise ValueError("frame_rows must not be negative")
        if self.cursor_row < 1 or self.cursor_col < 1:
            raise ValueError("cursor coordinates must be positive")


@dataclass(frozen=True)
class LayoutSnapshot:
    terminal_width: int
    terminal_height: int
    frame_start_row: int
    frame_rows: int
    regions: tuple[RegionGeometry, ...]
    source_slices: tuple[SourceSlice, ...]
    bottom: BottomGeometry
    cursor_row: int
    cursor_col: int
    scroll_epoch: int
    generation: int
    restore_epoch: int = 0

    def __post_init__(self) -> None:
        if self.terminal_width < 1 or self.terminal_height < 1:
            raise ValueError("terminal dimensions must be positive")
        if self.frame_start_row < 1:
            raise ValueError("frame_start_row must be positive")
        if self.frame_rows < 0 or self.frame_rows > self.terminal_height:
            raise ValueError("frame_rows exceeds terminal height")
        if self.frame_rows and self.frame_start_row + self.frame_rows - 1 > self.terminal_height:
            raise ValueError("frame extends beyond terminal height")
        if self.cursor_row < 1 or self.cursor_row > self.terminal_height:
            raise ValueError("cursor row exceeds terminal height")
        if self.cursor_col < 1 or self.cursor_col > self.terminal_width:
            raise ValueError("cursor column exceeds terminal width")
        if self.scroll_epoch < 0:
            raise ValueError("scroll_epoch must not be negative")
        if self.restore_epoch < 0:
            raise ValueError("restore_epoch must not be negative")
        if self.generation < 0:
            raise ValueError("generation must not be negative")
        if self.bottom.cursor_row != self.cursor_row or self.bottom.cursor_col != self.cursor_col:
            raise ValueError("snapshot and bottom cursor coordinates disagree")


@dataclass(frozen=True)
class LayoutDiff:
    kind: Literal["unchanged", "cursor", "regions", "suffix", "full"]
    first_region: str | None = None
    first_absolute_row: int | None = None
    changed_rows: tuple[int, ...] = ()
    old_tail_rows: tuple[int, ...] = ()
    reason: str = ""


def normalize_rendered_rows(
    ansi: str,
    *,
    width: int,
    signature_context: tuple[Any, ...] = (),
) -> RenderedRows:
    """Normalize one captured render without dropping meaningful trailing rows."""
    if width < 1:
        raise ValueError("width must be positive")
    if not ansi:
        rows: tuple[str, ...] = ()
    else:
        rows = tuple(ansi.split("\n"))
    return RenderedRows(
        ansi=ansi,
        rows=rows,
        visual_rows=len(rows),
        signature=(width, signature_context, rows),
        patch_safe=_is_patch_safe(ansi),
    )


def _is_patch_safe(value: str) -> bool:
    if "\r" in value:
        return False
    index = 0
    while index < len(value):
        if value[index] != "\x1b":
            index += 1
            continue
        if index + 1 >= len(value):
            return False
        if value[index + 1] == "[":
            match = _CSI_FINAL_RE.match(value, index)
            if match is None:
                return False
            if match.group(2) != "m":
                return False
            index = match.end()
            continue
        if value[index + 1] == "]":
            match = _OSC_RE.match(value, index)
            if match is None:
                return False
            index = match.end()
            continue
        return False
    return True


def map_source_cursor(
    source_slice: SourceSlice,
    *,
    source_row: int,
    bottom_start_row: int,
) -> int:
    """Map a 0-based source row to an absolute 1-based terminal row."""
    if source_slice.mode == "compact":
        raise ValueError("compact slices cannot map cursor rows")
    if not source_slice.source_start <= source_row < source_slice.source_end:
        raise ValueError("source cursor is outside the slice")
    if bottom_start_row < 1:
        raise ValueError("bottom_start_row must be positive")
    projected_row = source_slice.projected_start + source_row - source_slice.source_start
    return bottom_start_row + projected_row


def _empty_region(key: str, *, start_row: int, width: int) -> RegionGeometry:
    return RegionGeometry(
        key=key,
        start_row=max(start_row, 1),
        visual_rows=0,
        width=max(width, 1),
        content_signature=(),
        patch_safe=True,
    )


def build_bottom_geometry(
    children: Sequence[tuple[str, RenderedRows]],
    *,
    start_row: int,
    cursor_key: str,
    cursor_source_row: int,
    cursor_col: int = 1,
    projected_slices: Sequence[SourceSlice] = (),
    width: int | None = None,
) -> BottomGeometry:
    """Build bottom parent and child geometry from already projected children."""
    if start_row < 1:
        raise ValueError("bottom start_row must be positive")
    if not children:
        raise ValueError("bottom must contain at least one rendered child")
    child_map = dict(children)
    if cursor_key not in child_map:
        raise ValueError("cursor child is missing from bottom")
    cursor_rendered = child_map[cursor_key]

    rows: list[str] = []
    geometries: dict[str, RegionGeometry] = {}
    next_row = start_row
    effective_width = width or 1
    for key, rendered in children:
        region = RegionGeometry(
            key=f"bottom.{key}",
            start_row=next_row,
            visual_rows=rendered.visual_rows,
            width=effective_width,
            content_signature=rendered.signature,
            patch_safe=rendered.patch_safe,
        )
        geometries[key] = region
        rows.extend(rendered.rows)
        next_row += rendered.visual_rows
        effective_width = max(effective_width, 1)

    rendered = normalize_rendered_rows("\n".join(rows), width=effective_width)
    bottom_region = RegionGeometry(
        key="bottom",
        start_row=start_row,
        visual_rows=rendered.visual_rows,
        width=effective_width,
        content_signature=rendered.signature,
        patch_safe=all(region.patch_safe for region in geometries.values()),
    )
    empty = _empty_region("bottom.empty", start_row=next_row, width=effective_width)
    fields = {
        "top_separator": geometries.get("top_separator", empty),
        "input": geometries.get("input", empty),
        "middle_separator": geometries.get("middle_separator", empty),
        "panel": geometries.get("panel", empty),
        "panel_status_separator": geometries.get("panel_status_separator", empty),
        "status": geometries.get("status", empty),
    }
    source_slice = next(
        (
            item
            for item in projected_slices
            if item.key in {cursor_key, f"bottom.{cursor_key}"}
            and item.source_start <= cursor_source_row < item.source_end
        ),
        None,
    )
    if source_slice is None:
        if not 0 <= cursor_source_row < cursor_rendered.visual_rows:
            raise ValueError("cursor row is outside cursor child")
        projected_start = 0
        for key, child in children:
            if key == cursor_key:
                break
            projected_start += child.visual_rows
        source_slice = SourceSlice(
            key=f"bottom.{cursor_key}",
            source_start=0,
            source_end=cursor_rendered.visual_rows,
            projected_start=projected_start,
            projected_end=projected_start + cursor_rendered.visual_rows,
        )
    projected_cursor_row = (
        source_slice.projected_start
        + cursor_source_row
        - source_slice.source_start
    )
    if not 0 <= projected_cursor_row < rendered.visual_rows:
        raise ValueError("projected cursor row is outside bottom")
    cursor_row = start_row + projected_cursor_row
    return BottomGeometry(
        rendered=rendered,
        region=bottom_region,
        cursor_row=cursor_row,
        cursor_col=cursor_col,
        **fields,
    )


def project_bottom_viewport(
    source_children: Sequence[tuple[str, RenderedRows]],
    *,
    terminal_height: int,
    source_cursor_key: str,
    source_cursor_row: int,
    cursor_col: int = 1,
    start_row: int = 1,
    width: int = 1,
) -> BottomViewportPlan:
    """Project bottom rows while preserving a cursor-containing input window."""
    if terminal_height < 1:
        raise ValueError("terminal_height must be positive")
    children = tuple(source_children)
    child_map = dict(children)
    if source_cursor_key not in child_map:
        raise ValueError("cursor child is missing from bottom")
    cursor_child = child_map[source_cursor_key]
    if not 0 <= source_cursor_row < cursor_child.visual_rows:
        raise ValueError("source cursor row is outside cursor child")

    total_rows = sum(rendered.visual_rows for _, rendered in children)
    ranges: dict[str, list[tuple[int, int]]] = {key: [] for key, _ in children}
    if total_rows <= terminal_height:
        for key, rendered in children:
            if rendered.visual_rows:
                ranges[key] = [(0, rendered.visual_rows)]
    else:
        budget = terminal_height
        cursor_window = min(cursor_child.visual_rows, budget)
        cursor_start = max(0, min(source_cursor_row - cursor_window // 2, cursor_child.visual_rows - cursor_window))
        ranges[source_cursor_key] = [(cursor_start, cursor_start + cursor_window)]
        budget -= cursor_window
        priorities = ("panel", "status", "top_separator", "middle_separator", "panel_status_separator")
        for key in priorities + tuple(key for key, _ in children if key not in priorities and key != source_cursor_key):
            if budget <= 0 or key == source_cursor_key or key not in child_map:
                continue
            rendered = child_map[key]
            take = min(rendered.visual_rows, budget)
            if take:
                ranges[key] = [(rendered.visual_rows - take, rendered.visual_rows)]
                budget -= take

    selected_keys = {key for key, selected in ranges.items() if selected}
    separator_requirements = {
        "top_separator": ("input",),
        "middle_separator": (
            ("input", "panel")
            if "panel" in child_map
            else ("input",)
        ),
        "panel_status_separator": ("panel", "status"),
    }
    for separator, required_keys in separator_requirements.items():
        if separator in ranges and not all(key in selected_keys for key in required_keys):
            ranges[separator] = []

    projected_children: list[tuple[str, RenderedRows]] = []
    slices: list[SourceSlice] = []
    projected_row = 0
    for key, rendered in children:
        selected = ranges[key]
        if not selected:
            continue
        selected_rows: list[str] = []
        for source_start, source_end in selected:
            selected_rows.extend(rendered.rows[source_start:source_end])
            length = source_end - source_start
            slices.append(
                SourceSlice(
                    key=f"bottom.{key}",
                    source_start=source_start,
                    source_end=source_end,
                    projected_start=projected_row,
                    projected_end=projected_row + length,
                    mode="full",
                )
            )
            projected_row += length
        projected_children.append(
            (key, normalize_rendered_rows("\n".join(selected_rows), width=width))
        )

    cursor_slice = next(
        item
        for item in slices
        if item.key == f"bottom.{source_cursor_key}"
        and item.source_start <= source_cursor_row < item.source_end
    )
    projected_cursor_row = cursor_slice.projected_start + source_cursor_row - cursor_slice.source_start
    projected = build_bottom_geometry(
        projected_children,
        start_row=start_row,
        cursor_key=source_cursor_key,
        cursor_source_row=source_cursor_row,
        cursor_col=cursor_col,
        projected_slices=slices,
        width=width,
    )
    omitted_input_before = sum(
        start for start, _ in ranges[source_cursor_key]
    )
    omitted_input_after = cursor_child.visual_rows - sum(
        end - start for start, end in ranges[source_cursor_key]
    ) - omitted_input_before
    return BottomViewportPlan(
        source_signature=tuple((key, rendered.signature) for key, rendered in children),
        source_children=children,
        projected=projected,
        projected_slices=tuple(slices),
        source_cursor_key=source_cursor_key,
        source_cursor_row=source_cursor_row,
        projected_cursor_row=projected_cursor_row,
        omitted_input_before=max(omitted_input_before, 0),
        omitted_input_after=max(omitted_input_after, 0),
        panel_omitted="panel" in child_map and not ranges.get("panel"),
        status_omitted_rows=(
            child_map.get("status", normalize_rendered_rows("", width=width)).visual_rows
            - sum(end - start for start, end in ranges.get("status", ()))
        ),
    )




_TOP_REGION_KEYS = ("transcript", "todo", "vibe", "thinking")


def _empty_rendered_rows(width: int) -> RenderedRows:
    return normalize_rendered_rows("", width=width)


def _relocate_region(region: RegionGeometry, *, start_row: int) -> RegionGeometry:
    delta = start_row - region.start_row
    return replace(region, start_row=region.start_row + delta)


def _relocate_bottom_geometry(
    bottom: BottomGeometry,
    *,
    start_row: int,
) -> BottomGeometry:
    delta = start_row - bottom.region.start_row
    return replace(
        bottom,
        region=_relocate_region(bottom.region, start_row=start_row),
        top_separator=_relocate_region(
            bottom.top_separator,
            start_row=bottom.top_separator.start_row + delta,
        ),
        input=_relocate_region(
            bottom.input,
            start_row=bottom.input.start_row + delta,
        ),
        middle_separator=_relocate_region(
            bottom.middle_separator,
            start_row=bottom.middle_separator.start_row + delta,
        ),
        panel=_relocate_region(
            bottom.panel,
            start_row=bottom.panel.start_row + delta,
        ),
        panel_status_separator=_relocate_region(
            bottom.panel_status_separator,
            start_row=bottom.panel_status_separator.start_row + delta,
        ),
        status=_relocate_region(
            bottom.status,
            start_row=bottom.status.start_row + delta,
        ),
        cursor_row=bottom.cursor_row + delta,
    )


def project_physical_viewport(
    logical: LogicalRenderPlan,
    *,
    terminal_width: int,
    terminal_height: int,
    frame_start_row: int = 1,
) -> PhysicalViewportPlan:
    """Project a complete logical frame into a bounded physical viewport."""
    if terminal_width < 1:
        raise ValueError("terminal_width must be positive")
    if terminal_height < 1:
        raise ValueError("terminal_height must be positive")
    if frame_start_row < 1 or frame_start_row > terminal_height:
        raise ValueError("frame_start_row must fit inside terminal")
    if len(logical.source_regions) != len(_TOP_REGION_KEYS):
        raise ValueError("logical plan must contain transcript, todo, vibe, and thinking")

    capacity = terminal_height - frame_start_row + 1
    source_bottom = logical.bottom_source
    bottom_source = project_bottom_viewport(
        source_bottom.source_children,
        terminal_height=capacity,
        source_cursor_key=source_bottom.source_cursor_key,
        source_cursor_row=source_bottom.source_cursor_row,
        cursor_col=source_bottom.projected.cursor_col,
        start_row=1,
        width=terminal_width,
    )
    bottom_rows = bottom_source.projected.rendered.visual_rows
    if bottom_rows < 1 or bottom_rows > capacity:
        raise ValueError("projected bottom does not fit terminal viewport")

    remaining = capacity - bottom_rows
    selected_counts = [0] * len(_TOP_REGION_KEYS)
    for index in (3, 2, 1, 0):
        available = logical.source_regions[index].visual_rows
        selected = min(available, remaining)
        selected_counts[index] = selected
        remaining -= selected

    projected_regions: list[RenderedRows] = []
    source_slices: list[SourceSlice] = []
    top_rows = 0
    for index, (key, source) in enumerate(zip(_TOP_REGION_KEYS, logical.source_regions)):
        count = selected_counts[index]
        if count <= 0:
            projected_regions.append(_empty_rendered_rows(terminal_width))
            continue
        source_start = source.visual_rows - count
        mode = "full" if count == source.visual_rows else "tail"
        projected = normalize_rendered_rows(
            "\n".join(source.rows[source_start:]),
            width=terminal_width,
        )
        projected_regions.append(projected)
        source_slices.append(
            SourceSlice(
                key=key,
                source_start=source_start,
                source_end=source.visual_rows,
                projected_start=0,
                projected_end=count,
                mode=mode,
            )
        )
        top_rows += count

    bottom_start_row = frame_start_row + top_rows
    bottom = _relocate_bottom_geometry(
        bottom_source.projected,
        start_row=bottom_start_row,
    )
    frame_rows = top_rows + bottom.rendered.visual_rows
    if frame_rows < 1:
        raise ValueError("physical viewport must contain rows")
    if frame_start_row + frame_rows - 1 > terminal_height:
        raise ValueError("physical viewport exceeds terminal height")
    if not frame_start_row <= bottom.cursor_row <= terminal_height:
        raise ValueError("projected cursor is outside terminal viewport")

    source_slices.extend(bottom_source.projected_slices)
    return PhysicalViewportPlan(
        projected_regions=tuple(projected_regions),
        bottom=bottom,
        source_slices=tuple(source_slices),
        frame_rows=frame_rows,
        cursor_row=bottom.cursor_row,
        cursor_col=bottom.cursor_col,
        source_signature=logical.source_signature,
    )



def diff_layout(old: LayoutSnapshot | None, new: LayoutSnapshot) -> LayoutDiff:
    """Compare physical snapshots without considering generation."""
    if old is None:
        return LayoutDiff(kind="full", reason="no applied snapshot")
    if (
        old.terminal_width != new.terminal_width
        or old.terminal_height != new.terminal_height
        or old.frame_start_row != new.frame_start_row
        or old.scroll_epoch != new.scroll_epoch
    ):
        return LayoutDiff(kind="full", reason="terminal geometry or scroll epoch changed")
    if not _snapshot_patch_safe(old) or not _snapshot_patch_safe(new):
        return LayoutDiff(kind="full", reason="snapshot contains unsafe render controls")

    old_regions = {region.key: region for region in old.regions}
    new_regions = {region.key: region for region in new.regions}
    ordered_keys = [region.key for region in new.regions]
    if tuple(old_regions) != tuple(ordered_keys) or set(old_regions) != set(new_regions):
        first = new.regions[0] if new.regions else None
        return LayoutDiff(
            kind="suffix",
            first_region=first.key if first else None,
            first_absolute_row=first.start_row if first else None,
            reason="region order changed",
        )

    for key in ordered_keys:
        previous = old_regions[key]
        current = new_regions[key]
        if (
            previous.start_row != current.start_row
            or previous.visual_rows != current.visual_rows
        ):
            old_tail = ()
            if current.visual_rows < previous.visual_rows:
                old_tail = tuple(
                    range(
                        current.start_row + current.visual_rows,
                        previous.start_row + previous.visual_rows,
                    )
                )
            return LayoutDiff(
                kind="suffix",
                first_region=key,
                first_absolute_row=current.start_row,
                old_tail_rows=old_tail,
                reason="region geometry changed",
            )
        if previous.content_signature != current.content_signature:
            return LayoutDiff(
                kind="regions",
                first_region=key,
                first_absolute_row=current.start_row,
                changed_rows=tuple(
                    range(current.start_row, current.start_row + current.visual_rows)
                ),
                reason="region content changed",
            )

    if old.cursor_row != new.cursor_row or old.cursor_col != new.cursor_col:
        return LayoutDiff(kind="cursor", reason="cursor changed")
    return LayoutDiff(kind="unchanged", reason="physical snapshot unchanged")


def _snapshot_patch_safe(snapshot: LayoutSnapshot) -> bool:
    if not snapshot.bottom.region.patch_safe:
        return False
    return all(region.patch_safe for region in snapshot.regions)


__all__ = [
    "BottomGeometry",
    "BottomViewportPlan",
    "LayoutDiff",
    "LayoutSnapshot",
    "LogicalRenderPlan",
    "PhysicalViewportPlan",
    "RegionGeometry",
    "RenderedRows",
    "SourceSlice",
    "build_bottom_geometry",
    "diff_layout",
    "map_source_cursor",
    "normalize_rendered_rows",
    "project_bottom_viewport",
    "project_physical_viewport",
]
