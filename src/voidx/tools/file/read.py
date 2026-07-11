from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from pathlib import Path

from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    UserInteraction,
    UserResponse,
    model_to_json_schema,
    resolve_safe,
)
from .state import (
    covered_read_range,
    record_mtime,
    record_read_range,
)

from .types import BINARY_DETECTION_BYTES, BoundedReadOutput, DisplayLines, READ_OUTPUT_MAX_CHARS


def _split_display_lines(text: str) -> DisplayLines:
    if text == "":
        return DisplayLines([], False)
    trailing_newline = text.endswith("\n")
    if trailing_newline:
        text = text[:-1]
    return DisplayLines(text.split("\n"), trailing_newline)


def _split_edit_lines(text: str) -> list[str]:
    if text == "":
        return []
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def _join_display_lines(lines: list[str], *, trailing_newline: bool) -> str:
    if not lines:
        return "\n" if trailing_newline else ""
    text = "\n".join(lines)
    return f"{text}\n" if trailing_newline else text


def _read_continuation_note(next_offset: int) -> str:
    return (
        f"[Read output capped at {READ_OUTPUT_MAX_CHARS} chars. "
        f"Next unread line: {next_offset}. Use offset={next_offset} to continue.]"
    )


def _overlong_line_output(line_number: int, line: str, max_chars: int) -> str:
    note = (
        f"[Line {line_number} exceeds the read output budget "
        f"({READ_OUTPUT_MAX_CHARS} chars) and was not marked as read.]"
    )
    prefix = f"{line_number}\t"
    marker = "..."
    fragment_budget = max_chars - len(prefix) - len(marker) - 2 - len(note)
    if fragment_budget <= 0:
        return note[:max_chars]
    return f"{prefix}{line[:fragment_budget]}{marker}\n\n{note}"


def _numbered_output_with_note(numbered: list[str], next_offset: int) -> str:
    output = "\n".join(numbered)
    note = _read_continuation_note(next_offset)
    return f"{output}\n\n{note}" if output else note


def _binary_null_byte_detected(path) -> bool:
    with path.open("rb") as handle:
        return b"\0" in handle.read(BINARY_DETECTION_BYTES)


def _bounded_truncated_output(numbered: list[str], start_line: int, next_offset: int) -> BoundedReadOutput:
    return BoundedReadOutput(
        output=_numbered_output_with_note(numbered, next_offset),
        lines=len(numbered),
        end_line=start_line + len(numbered) - 1,
        next_offset=next_offset,
        truncated_by_chars=True,
        truncated_single_line=False,
    )


def _bounded_numbered_read_output(
    lines: list[str],
    start_line: int,
    *,
    max_chars: int = READ_OUTPUT_MAX_CHARS,
) -> BoundedReadOutput:
    if max_chars <= 0:
        return BoundedReadOutput(
            output="",
            lines=0,
            end_line=start_line - 1,
            next_offset=start_line if lines else None,
            truncated_by_chars=bool(lines),
            truncated_single_line=False,
        )
    numbered: list[str] = []
    output_len = 0
    for index, line in enumerate(lines):
        line_number = start_line + index
        rendered = f"{line_number}\t{line}"
        candidate_len = output_len + (1 if numbered else 0) + len(rendered)
        is_last_requested_line = index == len(lines) - 1
        if is_last_requested_line and candidate_len <= max_chars:
            numbered.append(rendered)
            output_len = candidate_len
            continue
        if not is_last_requested_line:
            next_offset = line_number + 1
            candidate_with_note_len = candidate_len + 2 + len(_read_continuation_note(next_offset))
            if candidate_with_note_len <= max_chars:
                numbered.append(rendered)
                output_len = candidate_len
                continue
        if not numbered:
            return BoundedReadOutput(
                output=_overlong_line_output(line_number, line, max_chars),
                lines=0,
                end_line=line_number - 1,
                next_offset=line_number,
                truncated_by_chars=True,
                truncated_single_line=True,
            )
        return _bounded_truncated_output(numbered, start_line, line_number)

    end_line = start_line + len(numbered) - 1
    return BoundedReadOutput(
        output="\n".join(numbered),
        lines=len(numbered),
        end_line=end_line,
        next_offset=None,
        truncated_by_chars=False,
        truncated_single_line=False,
    )




def _try_resolve_external(file_path: str) -> Path | None:
    """Resolve a path that may be outside workspace, expanding ~ and absolutizing.

    Returns the resolved Path if it points to a real file, or None otherwise.
    Does NOT perform sandbox checks — only used to determine if an external
    path is worth asking the user about.
    """
    try:
        raw = Path(file_path)
        if file_path.startswith("~") or raw.is_absolute():
            resolved = raw.expanduser().resolve()
        else:
            return None
        if resolved.is_file():
            return resolved
        return None
    except (OSError, ValueError):
        return None


class FileReadInput(BaseModel):
    file_path: str = Field(description="Path to the text file to read.")
    offset: int | None = Field(
        default=None,
        ge=1,
        description="1-based line number to start reading from; omit to start at line 1.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description="Maximum number of lines to read; omit to read until the output budget is reached.",
    )

    @field_validator("offset", mode="before")
    @classmethod
    def _normalize_zero_offset(cls, value):
        if value == 0 or value == "0":
            return 1
        return value


class FileReadTool(BaseTool):
    id = "read"
    description = "Read a text file and return visible content as numbered lines. Use offset/limit for large files or to continue a capped read."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileReadInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = FileReadInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is None:
            external = _try_resolve_external(inp.file_path)
            if external and ctx.interact:
                response = await ctx.interact(UserInteraction(
                    prompt=f"Read file outside workspace? {inp.file_path}",
                    options=[
                        ("Yes", "allow", "Allow this read once"),
                        ("No", "deny", "Do not read this file"),
                    ],
                ))
                if response.cancelled or response.value == "deny":
                    return ToolResult(
                        output=f"Read denied by user: {inp.file_path}",
                        metadata={"error": True},
                    )
                if ctx.add_extra_path:
                    ctx.add_extra_path(str(external.parent))
                path = external
            else:
                return ToolResult(
                    output=f"Path traversal blocked: {inp.file_path}",
                    metadata={"error": True},
                )
        if not path.exists():
            return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})
        if path.is_dir():
            return ToolResult(output=f"Path is a directory: {inp.file_path}", metadata={"error": True})
        if _binary_null_byte_detected(path):
            return ToolResult(
                title="Read blocked (binary file)",
                output=(
                    f"Cannot read {inp.file_path}: file appears to be binary "
                    f"(null byte found in first {BINARY_DETECTION_BYTES} bytes)."
                ),
                metadata={"file": inp.file_path, "lines": 0, "binary": True, "error": True},
            )

        display = _split_display_lines(path.read_text(encoding="utf-8", errors="replace"))
        lines = display.lines
        start = (inp.offset or 1) - 1
        if start >= len(lines):
            record_mtime(ctx, path)
            return ToolResult(
                title=f"Read 0 lines",
                output=f"Offset {inp.offset} is beyond end of file (file has {len(lines)} lines).",
                metadata={"file": inp.file_path, "lines": 0, "total_lines": len(lines), "error": True, "reason": "offset_beyond_eof"},
            )
        end = start + (inp.limit or len(lines))
        sliced = lines[start:end]
        requested_start = start + 1
        requested_end = start + len(sliced)
        covered_range = covered_read_range(ctx, path, requested_start, requested_end) if sliced else None
        if covered_range is not None:
            record_mtime(ctx, path)
            covered_lines = requested_end - requested_start + 1
            bounded = _bounded_numbered_read_output(sliced, start + 1)
            output = bounded.output
            return ToolResult(
                title=f"Read {bounded.lines} lines",
                output=output,
                summary=f"{bounded.lines}/{len(lines)} lines",
                metadata={
                    "file": inp.file_path,
                    "lines": bounded.lines,
                    "covered_lines": covered_lines,
                    "total_lines": len(lines),
                    "already_read": True,
                    "start_line": start + 1,
                    "end_line": bounded.end_line,
                    "next_offset": bounded.next_offset,
                    "truncated_by_chars": bounded.truncated_by_chars,
                    "truncated_single_line": bounded.truncated_single_line,
                },
            )

        bounded = _bounded_numbered_read_output(sliced, start + 1)

        record_mtime(ctx, path)
        if bounded.lines > 0:
            record_read_range(ctx, path, start + 1, bounded.end_line)

        return ToolResult(
            title=f"Read {bounded.lines} lines",
            output=bounded.output,
            summary=f"{bounded.lines}/{len(lines)} lines",
            metadata={
                "file": inp.file_path,
                "lines": bounded.lines,
                "total_lines": len(lines),
                "start_line": start + 1,
                "end_line": bounded.end_line,
                "next_offset": bounded.next_offset,
                "truncated_by_chars": bounded.truncated_by_chars,
                "truncated_single_line": bounded.truncated_single_line,
            },
        )
