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
