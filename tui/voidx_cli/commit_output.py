"""Absolute-positioned transcript output, with bounded hardware scrolling."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CommitOutput:
    ansi: str
    next_row: int
    lines_written: int
    scrolled_rows: int


def plan_commit(
    ansi: str, *, start_row: int, height: int, fixed_bottom_rows: int,
    previous_frame_rows: int = 0, previous_frame_start_row: int | None = None,
) -> CommitOutput | None:
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
            # CSI S may discard rows instead of saving history (e.g. xterm.js).
            payload.append(f"\x1b[1;{bottom}r\x1b[{bottom};1H\r\n\x1b[r")
            row = bottom
            scrolled += 1
        payload.append(f"\x1b[{row};1H{line}\x1b[K")
        row += 1
    owned_start = max(start_row, 1) if previous_frame_start_row is None else previous_frame_start_row
    owned_end = min(owned_start + max(previous_frame_rows, 0) - 1, height)
    for old_row in range(max(1, owned_start), owned_end + 1):
        mapped_row = old_row - scrolled if old_row <= bottom else old_row
        if row <= mapped_row <= bottom:
            payload.append(f"\x1b[{mapped_row};1H\x1b[K")
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
    for row in sorted(set(target) | set(physical)):
        line = target.get(row, "")
        if physical.get(row) != line:
            payload.append(f"\x1b[{row};1H{line}\x1b[K")
    return "".join(payload), len(payload)
