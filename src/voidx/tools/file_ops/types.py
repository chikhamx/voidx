from __future__ import annotations

from typing import Literal, NamedTuple

from pydantic import BaseModel, Field

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS


READ_OUTPUT_MAX_CHARS = DEFAULT_TOOL_MESSAGE_MAX_CHARS
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


class ParagraphResolution(NamedTuple):
    edits: list[ResolvedEdit]
    hints: list[str]


class BoundedReadOutput(NamedTuple):
    output: str
    lines: int
    end_line: int
    next_offset: int | None
    truncated_by_chars: bool
    truncated_single_line: bool


class EditEntry(BaseModel):
    operation: Literal["replace", "insert"] = Field(
        description=(
            "Edit operation. Use replace to replace a paragraph matched by prefix/suffix, "
            "or insert to add content after a matched paragraph."
        ),
    )
    lineno: int = Field(
        ge=0,
        description=(
            "Required search start hint. Use 1-based line numbers for normal edits; use 0 as a "
            "beginning-of-file hint. The tool searches within ±100 lines of this line for "
            "prefix/suffix matches. Not used as a precise target line."
        ),
    )
    prefix: str = Field(
        description=(
            "Text snippet that marks the beginning of the target paragraph. "
            "Too short may match the wrong location; too long may drift from the target. "
            "Aim for a distinctive 10-40 character snippet. "
            "Must not be empty, except for beginning-of-file insertion/prepend with lineno=0."
        ),
    )
    suffix: str = Field(
        description=(
            "Text snippet that marks the end of the target paragraph. "
            "Searched after the prefix, so it must not appear inside the prefix itself. "
            "Too short may match inside the prefix or earlier text; too long may overshoot. "
            "Aim for a distinctive 10-40 character snippet. "
            "Must not be empty, except for beginning-of-file insertion/prepend with lineno=0."
        ),
    )
    new_string: str = Field(
        description=(
            "Replacement or inserted content. A trailing newline does not add an extra blank line; "
            "start with a newline only when an intentional blank first line is desired."
        ),
    )
