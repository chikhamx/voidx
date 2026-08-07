from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class LineOverlap:
    head: int
    tail: int


def resolve_overlap(
    before_lines: Sequence[str],
    new_lines: Sequence[str],
    after_lines: Sequence[str],
    *,
    limit: int = 3,
) -> LineOverlap:
    head = 0
    for count in range(min(limit, len(before_lines), len(new_lines)), 0, -1):
        candidate = new_lines[:count]
        if "" not in candidate and list(before_lines[-count:]) == list(candidate):
            head = count
            break

    tail = 0
    remaining = len(new_lines) - head
    for count in range(min(limit, len(after_lines), remaining), 0, -1):
        candidate = new_lines[-count:]
        if "" not in candidate and list(after_lines[:count]) == list(candidate):
            tail = count
            break

    return LineOverlap(head=head, tail=tail)


@dataclass(frozen=True)
class CollapsedBlock:
    index: int
    size: int
    gap: int


@dataclass(frozen=True)
class CollapseResult:
    lines: list[str]
    collapsed: list[CollapsedBlock]


def collapse_adjacent_duplicate_blocks(
    lines: Sequence[str],
    *,
    boundaries: Sequence[int],
    margin: int = 3,
    min_block: int = 2,
    max_block: int = 3,
    max_gap_blanks: int = 1,
) -> CollapseResult:
    out = list(lines)
    n = len(out)
    spans = sorted(
        (max(0, b - margin), min(n, b + margin)) for b in boundaries
    )
    windows: list[list[int]] = []
    for start, end in spans:
        if end <= start:
            continue
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    collapsed: list[CollapsedBlock] = []
    shift = 0
    for window in windows:
        start = window[0] - shift
        end = window[1] - shift
        i = start
        while i < end:
            matched = False
            for k in range(min(max_block, (end - i) // 2), min_block - 1, -1):
                block = out[i:i + k]
                if "" in block:
                    continue
                if block == out[i + k:i + 2 * k]:
                    del out[i + k:i + 2 * k]
                    end -= k
                    collapsed.append(CollapsedBlock(index=i, size=k, gap=0))
                    matched = True
                    break
                for g in range(1, max_gap_blanks + 1):
                    j = i + k + g
                    if j + k > end:
                        break
                    if all(x == "" for x in out[i + k:j]) and out[j:j + k] == block:
                        del out[i + k:j + k]
                        end -= g + k
                        collapsed.append(CollapsedBlock(index=i, size=k, gap=g))
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                i += 1
        shift += (window[1] - window[0]) - (end - start)
    return CollapseResult(lines=out, collapsed=collapsed)
