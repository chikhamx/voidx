"""Pure line-range DTOs and remapping used by agent message policies."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiffSpan:
    old_start: int
    old_end: int
    offset: int


def remap_old_range(start: int, end: int, spans: list[DiffSpan]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = start
    cumulative_offset = 0
    for span in spans:
        if cursor > end:
            break
        if span.old_end < span.old_start:
            if span.old_start == 0:
                cumulative_offset += span.offset
                continue
            if cursor <= min(end, span.old_start):
                keep_end = min(end, span.old_start)
                ranges.append((cursor + cumulative_offset, keep_end + cumulative_offset))
                cursor = keep_end + 1
            cumulative_offset += span.offset
            continue
        if end < span.old_start:
            ranges.append((cursor + cumulative_offset, end + cumulative_offset))
            return ranges
        if cursor < span.old_start:
            keep_end = min(end, span.old_start - 1)
            ranges.append((cursor + cumulative_offset, keep_end + cumulative_offset))
            cursor = keep_end + 1
        if cursor <= span.old_end:
            cursor = span.old_end + 1
        cumulative_offset += span.offset
    if cursor <= end:
        ranges.append((cursor + cumulative_offset, end + cumulative_offset))
    return [(item_start, item_end) for item_start, item_end in ranges if item_start > 0 and item_end >= item_start]
