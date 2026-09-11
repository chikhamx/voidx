"""Absolute-positioned transcript output, with bounded hardware scrolling."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CommitOutput:
    ansi: str
    next_row: int
    lines_written: int
    scrolled_rows: int


def plan_commit(ansi: str, *, start_row: int, height: int, fixed_bottom_rows: int) -> CommitOutput | None:
    lines = ansi.split("\n")
    bottom = height - fixed_bottom_rows
    row = max(start_row, 1)
    overflow = row + len(lines) - 1 > bottom
    # Without an established protected bottom, there is no authorized scroll
    # region. Keep the transcript pending until a frame establishes one.
    if bottom < 1 or (overflow and (fixed_bottom_rows == 0 or bottom < 2)):
        return None
    payload: list[str] = []
    scrolled = 0
    for line in lines:
        if row > bottom:
            payload.append(f"\x1b[1;{bottom}r\x1b[{bottom};1H\x1b[1S\x1b[r")
            row = bottom
            scrolled += 1
        payload.append(f"\x1b[{row};1H{line}\x1b[K")
        row += 1
    return CommitOutput("".join(payload), row, len(lines), scrolled)


def scrolled_frame_payload(*, previous: list[str] | tuple[str, ...], previous_start: int, current: list[str] | tuple[str, ...], start: int, scroll_rows: int, scroll_bottom: int) -> tuple[str, int]:
    physical = {}
    for index, line in enumerate(previous):
        row = previous_start + index
        destination = row - scroll_rows if row <= scroll_bottom else row
        if destination >= 1:
            physical[destination] = line
    for row in range(max(1, scroll_bottom - scroll_rows + 1), scroll_bottom + 1):
        physical[row] = ""
    target = {start + index: line for index, line in enumerate(current)}
    payload = []
    for row in sorted(set(target) | {row for row in physical if row >= start}):
        line = target.get(row, "")
        if physical.get(row) != line:
            payload.append(f"\x1b[{row};1H{line}\x1b[K")
    return "".join(payload), len(payload)
