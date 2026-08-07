from __future__ import annotations

from typing import Literal, NamedTuple


from voidx.tooling.domain.output_policy import DEFAULT_TOOL_OUTPUT_MAX_CHARS


READ_OUTPUT_MAX_CHARS = DEFAULT_TOOL_OUTPUT_MAX_CHARS
BINARY_DETECTION_BYTES = 8 * 1024
TEXT_REPLACE_LINE_RADIUS = 3
TEXT_REPLACE_SPAN_TOLERANCE = 2


class DisplayLines(NamedTuple):
    lines: list[str]
    trailing_newline: bool


class ResolvedEdit(NamedTuple):
    operation: Literal["replace", "insert"]
    start_line: int
    end_line: int
    new_string: str



class BoundedReadOutput(NamedTuple):
    output: str
    lines: int
    end_line: int
    next_offset: int | None
    truncated_by_chars: bool
    truncated_single_line: bool

