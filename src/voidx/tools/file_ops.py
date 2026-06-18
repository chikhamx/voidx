"""File operation tools — read, write, edit. Deterministic, typed I/O."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field

from voidx.agent.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.diffing import FileDiff, make_file_diff, parse_unified_diff
from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult, resolve_safe
from voidx.tools.file_state import (
    check_read_coverage,
    check_staleness,
    clear_read_coverage,
    covered_read_range,
    file_fingerprint,
    record_mtime,
    record_read_range,
    remap_read_coverage_from_file_diff,
    save_file_version,
)


READ_OUTPUT_MAX_CHARS = DEFAULT_TOOL_MESSAGE_MAX_CHARS
BINARY_DETECTION_BYTES = 8 * 1024
TEXT_REPLACE_WINDOW_LINES = 30


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


class FileReadInput(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file")
    offset: int | None = Field(default=None, ge=1, description="Line number to start reading from (1-based)")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines to read")


class FileReadTool(BaseTool):
    id = "read"
    description = "Read a file. Returns content with line numbers. Use offset/limit for large files."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileReadInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileReadInput.model_validate(args)
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is None:
            return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
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
                metadata={"file": inp.file_path, "lines": 0, "total_lines": len(lines)},
            )
        end = start + (inp.limit or len(lines))
        sliced = lines[start:end]
        requested_start = start + 1
        requested_end = start + len(sliced)
        covered_range = covered_read_range(ctx, path, requested_start, requested_end) if sliced else None
        if covered_range is not None:
            record_mtime(ctx, path)
            covered_lines = requested_end - requested_start + 1
            note = (
                f"[Lines {requested_start}-{requested_end} were already read "
                f"(covered by prior read {covered_range.start_line}-{covered_range.end_line}). "
                "Content repeated for reference.]\n"
            )
            content_budget = READ_OUTPUT_MAX_CHARS - len(note)
            bounded = _bounded_numbered_read_output(sliced, start + 1, max_chars=content_budget)
            output = f"{note}{bounded.output}" if bounded.output else note.rstrip()
            return ToolResult(
                title=f"Read {bounded.lines} lines (already read)",
                output=output,
                summary=f"Already read {covered_lines}/{len(lines)} lines",
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
            summary=f"Read {bounded.lines}/{len(lines)} lines",
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


class FileWriteInput(BaseModel):
    file_path: str = Field(description="Path to write the file to")
    content: str = Field(
        description=(
            "Content to write. Keep under ~150 lines for best results; for larger files write "
            "a small non-empty skeleton with prefix/suffix markers, read it, then use edit to fill it incrementally."
        )
    )


class FileWriteTool(BaseTool):
    id = "write"
    description = (
        "Write content to a file. Creates parent directories. Overwrites existing files. "
        "For files around 150 lines or larger, write a skeleton first (imports, class/function "
        "signatures, docstrings, and prefix/suffix markers), read it, then use edit "
        "to replace or insert implementation blocks incrementally. This avoids output "
        "truncation and reduces wait time."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileWriteInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileWriteInput.model_validate(args)
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is None:
            return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
        if path.exists():
            stale = check_staleness(ctx, path)
            if stale:
                return ToolResult(output=stale, metadata={"error": True})

        old_content = ""
        if path.exists():
            await save_file_version(ctx, path, display_path=inp.file_path, tool_name=self.id)
            old_content = path.read_text(encoding="utf-8", errors="replace")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp.content, encoding="utf-8")
        size = len(inp.content)
        record_mtime(ctx, path)
        line_count = len(_split_display_lines(inp.content).lines)
        if line_count > 0:
            record_read_range(ctx, path, 1, line_count)
        else:
            clear_read_coverage(ctx, path)

        diff = make_file_diff(
            inp.file_path,
            old_content,
            inp.content,
            old_label=f"a/{inp.file_path}" if old_content else "/dev/null",
            new_label=f"b/{inp.file_path}",
        )

        output = f"File written: {inp.file_path} ({size} bytes)"
        line_count = len(_split_display_lines(inp.content).lines)
        if line_count > 200:
            output += (
                f"\nNote: This file is large ({line_count} lines). "
                "For future writes of similar size, consider writing a skeleton first "
                "with prefix/suffix markers, reading it, and using edit to add content incrementally."
            )

        return ToolResult(
            title=f"Wrote {size} bytes",
            output=output,
            summary=f"Wrote {size} bytes",
            metadata={"file": inp.file_path, "size": size},
            diff=diff,
        )


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
            "Text snippet that marks the beginning of the target paragraph. Can be a substring "
            "within a line or a short multi-line snippet. Must not be empty, except for "
            "beginning-of-file insertion/prepend with lineno=0."
        ),
    )
    suffix: str = Field(
        description=(
            "Text snippet that marks the end of the target paragraph. Can be a substring within "
            "a line or a short multi-line snippet. For single-line targets, prefix and suffix can "
            "match the same line. Must not be empty, except for beginning-of-file insertion/prepend "
            "with lineno=0."
        ),
    )
    new_string: str = Field(
        description=(
            "Replacement or inserted content. A trailing newline does not add an extra blank line; "
            "start with a newline only when an intentional blank first line is desired."
        )
    )


class FileEditInput(BaseModel):
    file_path: str = Field(description="Path to edit")
    edits: list[EditEntry] = Field(
        description=(
            "Prefix/suffix paragraph edits to apply atomically. Read the target lines first; lineno "
            "is a search hint and prefix/suffix are the locators."
        )
    )


class FileInsertInput(BaseModel):
    file_path: str = Field(description="Path to edit")
    lineno: int = Field(
        ge=-1,
        description=(
            "Insert after this 1-based line number. Use 0 to insert at the beginning of the file, "
            "or -1 to insert at the end."
        ),
    )
    new_string: str = Field(
        description=(
            "Content to insert. A trailing newline does not add an extra blank line; "
            "start with a newline only when an intentional blank first line is desired."
        )
    )


class FileReplaceInput(BaseModel):
    file_path: str = Field(description="Path to edit")
    lineno: int = Field(
        ge=1,
        description=(
            "Required search start hint. The tool searches within ±30 lines of this line "
            "for prefix/suffix text matches. Not used as a precise target line."
        ),
    )
    prefix: str = Field(
        description="Text snippet that marks the beginning of the target text segment.",
    )
    suffix: str = Field(
        description="Text snippet that marks the end of the target text segment.",
    )
    new_string: str = Field(
        description=(
            "Replacement content. A trailing newline does not add an extra blank line; "
            "start with a newline only when an intentional blank first line is desired."
        )
    )


class FileEditTool(BaseTool):
    id = "edit"
    description = (
        "Edit a single file by matching prefix/suffix text snippets near a required lineno hint. "
        "Use replace to replace the matched paragraph, or insert to add content after it. "
        "Use lineno=0 with empty prefix/suffix for beginning-of-file prepend. "
        "Multiple edits apply atomically from bottom to top."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileEditInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileEditInput.model_validate(args)
        if not inp.edits:
            return ToolResult(
                output="No edits provided. The 'edits' array must contain at least one entry.",
                metadata={"error": True},
            )
        return await _execute_paragraph_edits(
            ctx,
            file_path=inp.file_path,
            edit_entries=inp.edits,
            tool_name=self.id,
        )


class FileInsertTool(BaseTool):
    id = "insert"
    description = (
        "Insert content into a file.\n"
        "lineno=0 → insert at the beginning of the file.\n"
        "lineno=-1 → insert at the end of the file.\n"
        "lineno>0 → insert after that line number; read the target line first."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileInsertInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileInsertInput.model_validate(args)
        if inp.new_string == "":
            return ToolResult(
                title="No changes",
                output="Insertion content is empty; no changes applied.",
                summary="No changes",
                metadata={"file": inp.file_path, "operations": 0},
            )
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is None:
            return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
        resolved_lineno = inp.lineno
        if path.exists():
            total_lines = len(_split_display_lines(path.read_text(encoding="utf-8", errors="replace")).lines)
            if inp.lineno == -1:
                resolved_lineno = total_lines
            elif inp.lineno > total_lines:
                return ToolResult(
                    output=f"Cannot insert after line {inp.lineno}: file has {total_lines} lines.",
                    metadata={"error": True},
                )
        return await _execute_direct_edits(
            ctx,
            file_path=inp.file_path,
            edits=[ResolvedEdit("insert", resolved_lineno, resolved_lineno, inp.new_string)],
            tool_name=self.id,
        )


class FileReplaceTool(BaseTool):
    id = "replace"
    description = (
        "Replace thunk [prefix ... suffix] → new_string in a file. "
        "Searches nearest the lineno hint; replaces only the matched segment, not the whole line. "
        "Read the target lines first."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileReplaceInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileReplaceInput.model_validate(args)
        return await _execute_text_replace(
            ctx,
            file_path=inp.file_path,
            lineno=inp.lineno,
            prefix=inp.prefix,
            suffix=inp.suffix,
            new_string=inp.new_string,
            tool_name=self.id,
        )


def _resolve_edit_target(ctx: ToolContext, file_path: str):
    path = resolve_safe(ctx.workspace, file_path, ctx.sandbox_extra_paths)
    if path is None:
        return None, ToolResult(output=f"Path traversal blocked: {file_path}", metadata={"error": True})
    if not path.exists():
        return None, ToolResult(output=f"File not found: {file_path}", metadata={"error": True})
    stale = check_staleness(ctx, path)
    if stale:
        return None, ToolResult(output=stale, metadata={"error": True})
    return path, None


async def _execute_paragraph_edits(
    ctx: ToolContext,
    *,
    file_path: str,
    edit_entries: list[EditEntry],
    tool_name: str,
) -> ToolResult:
    path, error = _resolve_edit_target(ctx, file_path)
    if error is not None:
        return error
    assert path is not None

    original = path.read_text(encoding="utf-8", errors="replace")
    display = _split_display_lines(original)
    lines = list(display.lines)

    for i, edit in enumerate(edit_entries):
        if edit.operation == "insert" and edit.new_string == "":
            return ToolResult(
                title="No changes",
                output=f"Edit {i}: insertion content is empty; no changes applied.",
                summary="No changes",
                metadata={"file": file_path, "operations": 0},
            )

    resolution = _resolve_paragraph_edits(lines, edit_entries)
    if isinstance(resolution, str):
        return ToolResult(output=resolution, metadata={"error": True})
    return await _apply_resolved_edits(
        ctx,
        path=path,
        file_path=file_path,
        edits=resolution.edits,
        original=original,
        display=display,
        tool_name=tool_name,
        hints=resolution.hints,
    )


async def _execute_direct_edits(
    ctx: ToolContext,
    *,
    file_path: str,
    edits: list[ResolvedEdit],
    tool_name: str,
) -> ToolResult:
    path, error = _resolve_edit_target(ctx, file_path)
    if error is not None:
        return error
    assert path is not None

    original = path.read_text(encoding="utf-8", errors="replace")
    display = _split_display_lines(original)
    return await _apply_resolved_edits(
        ctx,
        path=path,
        file_path=file_path,
        edits=edits,
        original=original,
        display=display,
        tool_name=tool_name,
        hints=[],
    )


async def _execute_text_replace(
    ctx: ToolContext,
    *,
    file_path: str,
    lineno: int,
    prefix: str,
    suffix: str,
    new_string: str,
    tool_name: str,
) -> ToolResult:
    path, error = _resolve_edit_target(ctx, file_path)
    if error is not None:
        return error
    assert path is not None

    original = path.read_text(encoding="utf-8", errors="replace")
    display = _split_display_lines(original)
    match = _find_text_segment(display.lines, lineno, prefix, suffix)
    if isinstance(match, str):
        return ToolResult(output=match, metadata={"error": True})

    start_offset, end_offset, start_line, end_line = match
    coverage_error = check_read_coverage(ctx, path, start_line, end_line)
    if coverage_error:
        return ToolResult(output=f"Edit 0: {coverage_error}", metadata={"error": True})

    key = str(path.resolve())
    existing_coverage = ctx.file_read_coverage.get(key, {})
    current_fingerprint = asdict(file_fingerprint(path))
    old_ranges = [
        item.copy()
        for item in existing_coverage.get("ranges", [])
    ] if existing_coverage.get("fingerprint") == current_fingerprint else []

    content = f"{original[:start_offset]}{new_string}{original[end_offset:]}"
    await save_file_version(ctx, path, display_path=file_path, tool_name=tool_name)
    path.write_text(content, encoding="utf-8")
    diff = make_file_diff(file_path, original, content)
    parsed = parse_unified_diff(diff)
    file_diff = parsed.files[0] if parsed.files else FileDiff(path=file_path)
    remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)

    output = f"File edited: {file_path} (1 operations)"
    if diff:
        output = f"{output}\n{diff}"
    return ToolResult(
        title="Edited (1 edits)",
        output=output,
        summary="Edited (1 operations)",
        metadata={"file": file_path, "operations": 1},
        diff=diff,
    )


async def _apply_resolved_edits(
    ctx: ToolContext,
    *,
    path,
    file_path: str,
    edits: list[ResolvedEdit],
    original: str,
    display: DisplayLines,
    tool_name: str,
    hints: list[str],
) -> ToolResult:
    lines = list(display.lines)
    total_lines = len(lines)
    validation_error = _validate_resolved_edits(edits, total_lines)
    if validation_error:
        return ToolResult(output=validation_error, metadata={"error": True})

    for i, edit in enumerate(edits):
        # (0, 0) is the convention for beginning-of-file insert/prepend —
        # no prior read is required since there are no existing lines to verify.
        if (edit.start_line, edit.end_line) == (0, 0):
            continue
        coverage_error = check_read_coverage(ctx, path, edit.start_line, edit.end_line)
        if coverage_error:
            return ToolResult(output=f"Edit {i}: {coverage_error}", metadata={"error": True})

    key = str(path.resolve())
    existing_coverage = ctx.file_read_coverage.get(key, {})
    current_fingerprint = asdict(file_fingerprint(path))
    old_ranges = [
        item.copy()
        for item in existing_coverage.get("ranges", [])
    ] if existing_coverage.get("fingerprint") == current_fingerprint else []

    trailing_newline = _result_trailing_newline(edits, total_lines, display.trailing_newline)
    for edit in sorted(edits, key=lambda item: item.start_line, reverse=True):
        new_lines = _split_edit_lines(edit.new_string)
        if edit.operation == "replace":
            if (edit.start_line, edit.end_line) == (0, 0):
                lines[0:0] = new_lines
            else:
                lines[edit.start_line - 1:edit.end_line] = new_lines
        else:
            lines[edit.start_line:edit.start_line] = new_lines

    content = _join_display_lines(lines, trailing_newline=trailing_newline)

    await save_file_version(ctx, path, display_path=file_path, tool_name=tool_name)
    path.write_text(content, encoding="utf-8")
    diff = make_file_diff(file_path, original, content)
    parsed = parse_unified_diff(diff)
    file_diff = parsed.files[0] if parsed.files else FileDiff(path=file_path)
    remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)

    details = "\n".join([*hints, *_line_shift_hints(edits), diff])
    output = f"File edited: {file_path} ({len(edits)} operations)"
    if details:
        output = f"{output}\n{details}"

    return ToolResult(
        title=f"Edited ({len(edits)} edits)",
        output=output,
        summary=f"Edited ({len(edits)} operations)",
        metadata={"file": file_path, "operations": len(edits)},
        diff=diff,
    )


def _validate_resolved_edits(edits: list[ResolvedEdit], total_lines: int) -> str | None:
    replacements: list[tuple[int, int]] = []
    insertion_locations: set[int] = set()
    for i, edit in enumerate(edits):
        if edit.operation == "replace":
            if (edit.start_line, edit.end_line) == (0, 0):
                insertion_locations.add(0)
                continue
            if edit.start_line < 1 or edit.end_line > total_lines:
                return f"Edit {i}: line number out of range for file with {total_lines} lines."
            if edit.end_line < edit.start_line:
                return f"Edit {i}: start_line must be <= end_line."
            replacements.append((edit.start_line, edit.end_line))
        else:
            if edit.start_line < 0 or edit.start_line > total_lines:
                return f"Edit {i}: line number out of range for file with {total_lines} lines."
            if edit.start_line in insertion_locations:
                return f"Edit {i}: multiple insertions at the same resolved location are ambiguous."
            insertion_locations.add(edit.start_line)

    for i, (start, end) in enumerate(replacements):
        for other_start, other_end in replacements[i + 1:]:
            if start <= other_end and other_start <= end:
                return "Edit ranges must not overlap."
        for location in insertion_locations:
            if start <= location <= end:
                return "Insert location must not be inside a replacement range."
    return None


def _find_text_segment(
    lines: list[str],
    lineno: int,
    prefix: str,
    suffix: str,
) -> tuple[int, int, int, int] | str:
    if prefix == "" or suffix == "":
        return "prefix and suffix must not be empty."

    total_lines = len(lines)
    window_start = max(1, lineno - TEXT_REPLACE_WINDOW_LINES)
    window_end = min(total_lines, lineno + TEXT_REPLACE_WINDOW_LINES)
    if window_start > window_end:
        return (
            f"prefix {prefix!r} not found within ±{TEXT_REPLACE_WINDOW_LINES} "
            f"lines of line {lineno}. Read the file to get current content."
        )

    text, line_starts = _window_text(lines, window_start, window_end)
    matches = _find_snippet_matches(text, line_starts, window_start, prefix)
    if not matches:
        return (
            f"prefix {prefix!r} not found within ±{TEXT_REPLACE_WINDOW_LINES} "
            f"lines of line {lineno}. Read the file to get current content."
        )

    distances = [abs(match_line - lineno) for _, match_line in matches]
    min_distance = min(distances)
    nearest = [
        match
        for match, distance in zip(matches, distances)
        if distance == min_distance
    ]
    if len(nearest) > 1:
        nearest_lines = sorted({line for _, line in nearest})
        return (
            f"prefix {prefix!r} is ambiguous at lines {_format_lines(nearest_lines)}. "
            "Provide a more specific prefix or adjust lineno."
        )

    prefix_offset, start_line = nearest[0]
    suffix_offset = text.find(suffix, prefix_offset)
    if suffix_offset == -1:
        return f"suffix {suffix!r} not found after prefix at line {start_line}. Read the file to get current content."

    suffix_end_offset = suffix_offset + len(suffix)
    end_line = _line_for_offset(line_starts, window_start, suffix_end_offset - 1)
    window_global_offset = _global_offset_for_line(lines, window_start)
    return (
        window_global_offset + prefix_offset,
        window_global_offset + suffix_end_offset,
        start_line,
        end_line,
    )


def _resolve_paragraph_edits(lines: list[str], edits: list[EditEntry]) -> ParagraphResolution | str:
    resolved: list[ResolvedEdit] = []
    for i, edit in enumerate(edits):
        found = _find_paragraph(lines, edit.operation, edit.lineno, edit.prefix, edit.suffix)
        if isinstance(found, str):
            return f"Edit {i}: {found}"
        start_line, end_line = found
        resolved.append(ResolvedEdit(edit.operation, start_line, end_line, edit.new_string))
    return ParagraphResolution(resolved, [])


def _find_paragraph(
    lines: list[str],
    operation: Literal["replace", "insert"],
    lineno: int,
    prefix: str,
    suffix: str,
) -> tuple[int, int] | str:
    del operation
    if lineno == 0 and prefix == "" and suffix == "":
        return (0, 0)
    if prefix == "" or suffix == "":
        return "prefix and suffix must not be empty (except beginning-of-file insertion/prepend with lineno=0)."

    total_lines = len(lines)
    if lineno == 0:
        window_start, window_end = 1, min(total_lines, 100)
    else:
        window_start = max(1, lineno - 100)
        window_end = min(total_lines, lineno + 100)
    if window_start > window_end:
        return f"prefix {prefix!r} not found within ±100 lines of line {lineno}. Read the file to get current content."

    text, line_starts = _window_text(lines, window_start, window_end)
    matches = _find_snippet_matches(text, line_starts, window_start, prefix)
    if not matches:
        return f"prefix {prefix!r} not found within ±100 lines of line {lineno}. Read the file to get current content."

    target_line = 0 if lineno == 0 else lineno
    distances = [abs(match_line - target_line) for _, match_line in matches]
    min_distance = min(distances)
    nearest = [
        match
        for match, distance in zip(matches, distances)
        if distance == min_distance
    ]
    nearest_lines = sorted({line for _, line in nearest})
    if len(nearest_lines) > 1:
        return f"prefix {prefix!r} is ambiguous at lines {_format_lines(nearest_lines)}. Provide a more specific prefix or adjust lineno."

    prefix_offset, start_line = nearest[0]
    suffix_offset = text.find(suffix, prefix_offset)
    if suffix_offset == -1:
        return f"suffix {suffix!r} not found after prefix at line {start_line}. Read the file to get current content."
    suffix_end_offset = suffix_offset + len(suffix) - 1
    end_line = _line_for_offset(line_starts, window_start, suffix_end_offset)
    return (start_line, end_line)


def _window_text(lines: list[str], start_line: int, end_line: int) -> tuple[str, list[int]]:
    selected = lines[start_line - 1:end_line]
    starts: list[int] = []
    offset = 0
    for i, line in enumerate(selected):
        starts.append(offset)
        offset += len(line)
        if i < len(selected) - 1:
            offset += 1
    return "\n".join(selected), starts


def _find_snippet_matches(text: str, line_starts: list[int], window_start: int, snippet: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    offset = text.find(snippet)
    while offset != -1:
        matches.append((offset, _line_for_offset(line_starts, window_start, offset)))
        offset = text.find(snippet, offset + 1)
    return matches


def _global_offset_for_line(lines: list[str], line_number: int) -> int:
    if line_number <= 1:
        return 0
    return len("\n".join(lines[:line_number - 1])) + 1


def _line_for_offset(line_starts: list[int], window_start: int, offset: int) -> int:
    index = bisect_right(line_starts, offset) - 1
    return window_start + max(index, 0)


def _format_lines(lines: list[int]) -> str:
    return ", ".join(str(line) for line in lines)


def _line_shift_hints(edits: list[ResolvedEdit]) -> list[str]:
    hints: list[str] = []
    for edit in sorted(edits, key=lambda item: item.start_line):
        new_count = len(_split_edit_lines(edit.new_string))
        if edit.operation == "replace":
            old_count = 0 if (edit.start_line, edit.end_line) == (0, 0) else edit.end_line - edit.start_line + 1
            offset = new_count - old_count
            if offset:
                if edit.end_line == 0:
                    hints.append(f"Line shift: all existing lines shifted by {offset:+d}")
                else:
                    hints.append(f"Line shift: lines after {edit.end_line} shifted by {offset:+d}")
        else:
            if new_count == 0:
                continue
            if edit.start_line == 0:
                hints.append(f"Line shift: all existing lines shifted by {new_count:+d}")
            else:
                hints.append(f"Line shift: lines after {edit.start_line} shifted by {new_count:+d}")
    return hints


def _result_trailing_newline(edits: list[ResolvedEdit], total_lines: int, original_trailing_newline: bool) -> bool:
    trailing_newline = original_trailing_newline
    for edit in edits:
        touches_final = (
            edit.operation == "replace" and edit.end_line == total_lines
        ) or (
            edit.operation == "insert" and edit.start_line == total_lines
        )
        if touches_final:
            trailing_newline = edit.new_string.endswith("\n")
    return trailing_newline
