"""Applied geometry planned from a confirmed pre-commit snapshot."""

from dataclasses import dataclass

from .commit_output import CommitOutput


@dataclass(frozen=True)
class CommitGeometry:
    visible_rows: int
    next_row: int
    remaining_frame_rows: int


def plan_commit_geometry(
    output: CommitOutput,
    *,
    visible_before: int,
    height: int,
    fixed_bottom_rows: int,
    previous_frame_start_row: int,
    previous_frame_rows: int,
) -> CommitGeometry:
    bottom = height - fixed_bottom_rows
    visible_after = min(bottom, max(0, visible_before + output.lines_written - output.scrolled_rows))
    if visible_after != output.next_row - 1:
        raise ValueError("commit output disagrees with confirmed history geometry")
    previous_end = min(height, previous_frame_start_row + previous_frame_rows - 1)
    remaining_end = output.next_row - 1
    for row in range(max(1, previous_frame_start_row), previous_end + 1):
        mapped = row - output.scrolled_rows if row <= bottom else row
        if mapped >= output.next_row:
            remaining_end = max(remaining_end, mapped)
    return CommitGeometry(output.next_row - 1, output.next_row, remaining_end - output.next_row + 1)
