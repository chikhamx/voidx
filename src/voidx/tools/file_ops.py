"""File operation tools — read, write, edit. Deterministic, typed I/O."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field

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


class DisplayLines(NamedTuple):
    lines: list[str]
    trailing_newline: bool


class AnchorResolution(NamedTuple):
    edits: list["EditEntry"]
    hints: list[str]


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
            output = (
                f"Lines {requested_start}-{requested_end} in {inp.file_path} were already read "
                f"from the current file version (covered by prior read {covered_range.start_line}-{covered_range.end_line}). "
                "Use the previous read output; no content repeated."
            )
            return ToolResult(
                title="Read skipped (already read)",
                output=output,
                summary=f"Already read {covered_lines}/{len(lines)} lines",
                metadata={
                    "file": inp.file_path,
                    "lines": 0,
                    "covered_lines": covered_lines,
                    "total_lines": len(lines),
                    "already_read": True,
                },
            )

        numbered = []
        for i, line in enumerate(sliced, start=start + 1):
            numbered.append(f"{i}\t{line}")

        record_mtime(ctx, path)
        if sliced:
            record_read_range(ctx, path, start + 1, start + len(sliced))

        return ToolResult(
            title=f"Read {len(sliced)} lines",
            output="\n".join(numbered),
            summary=f"Read {len(sliced)}/{len(lines)} lines",
            metadata={"file": inp.file_path, "lines": len(sliced), "total_lines": len(lines)},
        )


class FileWriteInput(BaseModel):
    file_path: str = Field(description="Path to write the file to")
    content: str = Field(
        description=(
            "Content to write. Keep under ~150 lines for best results; for larger files write "
            "a small non-empty skeleton with anchor lines, read it, then use edit to fill it incrementally."
        )
    )


class FileWriteTool(BaseTool):
    id = "write"
    description = (
        "Write content to a file. Creates parent directories. Overwrites existing files. "
        "For files around 150 lines or larger, write a skeleton first (imports, class/function "
        "signatures, docstrings, and anchor placeholders), read it for line numbers, then use edit "
        "to replace anchors or insert implementation blocks incrementally. This avoids output "
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
                "with anchor lines, reading it, and using edit to add content incrementally."
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
            "Edit operation. Use replace for an inclusive line range, or insert to add content "
            "after start_line. For insert, start_line=0 inserts at the beginning of the file."
        ),
    )
    start_line: int = Field(description="1-based replacement start line or insert-after anchor line; 0 means file start for insert")
    end_line: int | None = Field(
        default=None,
        description="1-based replacement end line, inclusive. Required for replace; omitted for insert.",
    )
    new_string: str = Field(
        description=(
            "Replacement or inserted content. A trailing newline does not add an extra blank line; "
            "start with a newline only when an intentional blank first line is desired."
        )
    )
    scope: str | None = Field(
        default=None,
        description="Optional nearby stable line, such as a function/class definition, used to limit anchor search.",
    )
    anchor: str | None = Field(
        default=None,
        description=(
            "Optional expected content at start_line. If it does not match, edit searches and "
            "corrects the line when unique, or returns an ambiguity error when multiple matches exist."
        ),
    )


class FileEditInput(BaseModel):
    file_path: str = Field(description="Path to edit")
    edits: list[EditEntry] = Field(
        description=(
            "Line-based edits to apply atomically. Read the target lines first; ranges use 1-based line numbers."
        )
    )


class FileEditTool(BaseTool):
    id = "edit"
    description = (
        "Edit a single file by 1-based line numbers after reading the target lines. "
        "Use replace with start_line/end_line, or insert to add content after start_line "
        "(start_line=0 inserts at the beginning). For edits after line shifts, provide "
        "anchor and optionally scope so the tool can verify or correct the target line. "
        "Multiple edits apply atomically from bottom to top."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileEditInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileEditInput.model_validate(args)
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is None:
            return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
        if not path.exists():
            return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})

        if not inp.edits:
            return ToolResult(
                output="No edits provided. The 'edits' array must contain at least one entry.",
                metadata={"error": True},
            )

        stale = check_staleness(ctx, path)
        if stale:
            return ToolResult(output=stale, metadata={"error": True})

        original = path.read_text(encoding="utf-8", errors="replace")
        display = _split_display_lines(original)
        lines = list(display.lines)
        total_lines = len(lines)

        validation_error = _validate_line_edits(inp.edits, total_lines)
        if validation_error:
            return ToolResult(output=validation_error, metadata={"error": True})

        resolution = _resolve_anchored_edits(lines, inp.edits)
        if isinstance(resolution, str):
            return ToolResult(output=resolution, metadata={"error": True})
        edits = resolution.edits

        validation_error = _validate_line_edits(edits, total_lines)
        if validation_error:
            return ToolResult(output=validation_error, metadata={"error": True})

        for i, edit in enumerate(edits):
            if edit.operation == "insert" and edit.start_line == 0:
                continue
            range_end = edit.end_line if edit.operation == "replace" else edit.start_line
            coverage_error = check_read_coverage(ctx, path, edit.start_line, range_end or edit.start_line)
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
                assert edit.end_line is not None
                lines[edit.start_line - 1:edit.end_line] = new_lines
            else:
                lines[edit.start_line:edit.start_line] = new_lines

        content = _join_display_lines(lines, trailing_newline=trailing_newline)

        await save_file_version(ctx, path, display_path=inp.file_path, tool_name=self.id)
        path.write_text(content, encoding="utf-8")
        diff = make_file_diff(inp.file_path, original, content)
        parsed = parse_unified_diff(diff)
        file_diff = parsed.files[0] if parsed.files else FileDiff(path=inp.file_path)
        remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)

        hints = [*resolution.hints, *_line_shift_hints(edits)]
        details = "\n".join([*hints, diff]) if hints else diff
        output = f"File edited: {inp.file_path} ({len(edits)} operations)"
        if details:
            output = f"{output}\n{details}"

        return ToolResult(
            title=f"Edited ({len(edits)} edits)",
            output=output,
            summary=f"Edited ({len(edits)} operations)",
            metadata={"file": inp.file_path, "operations": len(edits)},
            diff=diff,
        )


def _validate_line_edits(edits: list[EditEntry], total_lines: int) -> str | None:
    replacements: list[tuple[int, int]] = []
    insertion_anchors: set[int] = set()
    for i, edit in enumerate(edits):
        if edit.operation == "replace":
            if edit.start_line < 1 or edit.start_line > total_lines:
                return f"Edit {i}: line number out of range for file with {total_lines} lines."
            if edit.end_line is None:
                return f"Edit {i}: end_line is required for replace."
            if edit.end_line < edit.start_line:
                return f"Edit {i}: start_line must be <= end_line."
            if edit.end_line > total_lines:
                return f"Edit {i}: line number out of range for file with {total_lines} lines."
            replacements.append((edit.start_line, edit.end_line))
        else:
            if edit.start_line < 0 or edit.start_line > total_lines:
                return f"Edit {i}: line number out of range for file with {total_lines} lines."
            if edit.end_line is not None:
                return f"Edit {i}: end_line must be omitted for insertions."
            if edit.new_string == "":
                return f"Edit {i}: insertion content must not be empty."
            if edit.start_line == 0 and edit.anchor:
                return f"Edit {i}: anchor cannot be used with insert at start_line 0."
            if edit.start_line in insertion_anchors:
                return f"Edit {i}: multiple insertions at the same anchor are ambiguous."
            insertion_anchors.add(edit.start_line)

    for i, (start, end) in enumerate(replacements):
        for other_start, other_end in replacements[i + 1:]:
            if start <= other_end and other_start <= end:
                return "Edit ranges must not overlap."
        for anchor in insertion_anchors:
            if start <= anchor <= end:
                return "Insertion anchor must not be inside a replacement range."
    return None


def _resolve_anchored_edits(lines: list[str], edits: list[EditEntry]) -> AnchorResolution | str:
    resolved: list[EditEntry] = []
    hints: list[str] = []
    for i, edit in enumerate(edits):
        scope_bounds: tuple[int, int] | None = None
        if edit.scope:
            scope_matches = _find_text_lines(lines, edit.scope)
            if not scope_matches:
                return f"Edit {i}: scope {edit.scope!r} not found. Read the file to get current content."
            if len(scope_matches) > 1:
                return f"Edit {i}: scope {edit.scope!r} is ambiguous at lines {_format_lines(scope_matches)}."
            scope_bounds = _scope_search_bounds(lines, scope_matches[0])

        if not edit.anchor:
            resolved.append(edit)
            continue

        if 1 <= edit.start_line <= len(lines) and edit.anchor in lines[edit.start_line - 1]:
            resolved.append(edit)
            continue

        search_start, search_end = scope_bounds if scope_bounds is not None else (1, len(lines))
        matches = _find_text_lines(lines, edit.anchor, start_line=search_start, end_line=search_end)
        if not matches:
            return f"Edit {i}: anchor {edit.anchor!r} not found. Read the file to get current content."
        if len(matches) > 1:
            return f"Edit {i}: anchor {edit.anchor!r} is ambiguous at lines {_format_lines(matches)}."

        new_start = matches[0]
        old_start = edit.start_line
        update = {"start_line": new_start}
        if edit.operation == "replace":
            assert edit.end_line is not None
            update["end_line"] = new_start + (edit.end_line - edit.start_line)
        corrected = edit.model_copy(update=update)
        hints.append(
            f"Line corrected: edit {i} start_line {old_start} -> {new_start} "
            f"(matched anchor {edit.anchor!r})"
        )
        resolved.append(corrected)
    return AnchorResolution(resolved, hints)


def _find_text_lines(lines: list[str], text: str, *, start_line: int = 1, end_line: int | None = None) -> list[int]:
    if end_line is None:
        end_line = len(lines)
    start_line = max(start_line, 1)
    end_line = min(end_line, len(lines))
    return [
        lineno
        for lineno in range(start_line, end_line + 1)
        if text in lines[lineno - 1]
    ]


def _scope_search_bounds(lines: list[str], scope_line: int) -> tuple[int, int]:
    if scope_line < 1 or scope_line > len(lines):
        return 1, len(lines)
    scope_text = lines[scope_line - 1]
    if "{" in scope_text and scope_text.count("{") > scope_text.count("}"):
        balance = 0
        brace_end: int | None = None
        for lineno in range(scope_line, len(lines) + 1):
            balance += lines[lineno - 1].count("{")
            balance -= lines[lineno - 1].count("}")
            if lineno > scope_line and balance <= 0:
                brace_end = lineno
                break
        if brace_end is not None:
            return scope_line, brace_end
        # No closing brace was found. Fall through to the text/indent fallback below.
    stripped = scope_text.strip()
    if stripped.endswith(":"):
        indent = _indent_width(scope_text)
        for lineno in range(scope_line + 1, len(lines) + 1):
            line = lines[lineno - 1]
            if line.strip() and _indent_width(line) <= indent:
                return scope_line, lineno - 1
        return scope_line, len(lines)

    indent = _indent_width(scope_text)
    for lineno in range(scope_line + 1, len(lines) + 1):
        line = lines[lineno - 1]
        if line.strip() and _indent_width(line) <= indent and _looks_like_structure_start(line):
            return scope_line, lineno - 1
    return scope_line, len(lines)


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _looks_like_structure_start(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith((
        "async def ",
        "def ",
        "class ",
        "function ",
        "fn ",
        "struct ",
        "enum ",
        "interface ",
        "type ",
    ))


def _format_lines(lines: list[int]) -> str:
    return ", ".join(str(line) for line in lines)


def _line_shift_hints(edits: list[EditEntry]) -> list[str]:
    hints: list[str] = []
    for edit in sorted(edits, key=lambda item: item.start_line):
        new_count = len(_split_edit_lines(edit.new_string))
        if edit.operation == "replace":
            assert edit.end_line is not None
            offset = new_count - (edit.end_line - edit.start_line + 1)
            if offset:
                hints.append(f"Line shift: lines after {edit.end_line} shifted by {offset:+d}")
        else:
            if new_count == 0:
                continue
            if edit.start_line == 0:
                hints.append(f"Line shift: all existing lines shifted by {new_count:+d}")
            else:
                hints.append(f"Line shift: lines after {edit.start_line} shifted by {new_count:+d}")
    return hints


def _result_trailing_newline(edits: list[EditEntry], total_lines: int, original_trailing_newline: bool) -> bool:
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
