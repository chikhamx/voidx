from __future__ import annotations

from dataclasses import asdict

from pydantic import BaseModel, Field, model_validator

from voidx.diffing import FileDiff, make_file_diff, parse_unified_diff
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
from voidx.tools.file_state import (
    check_read_coverage,
    check_staleness,
    file_fingerprint,
    remap_read_coverage_from_file_diff,
    save_file_version,
)

from .edit_resolve import (
    _find_text_segment,
    _resolve_paragraph_edits,
    _result_trailing_newline,
    _validate_resolved_edits,
)
from .read import _join_display_lines, _split_display_lines, _split_edit_lines
from .types import DisplayLines, EditEntry, ParagraphResolution, ResolvedEdit


class FileEditInput(BaseModel):
    file_path: str = Field(description="Path to edit")
    edits: list[EditEntry] = Field(
        description=(
            "Prefix/suffix paragraph edits to apply atomically. Read the target lines first; lineno "
            "is a search hint and prefix/suffix are the locators."
        )
    )


class FileInsertInput(BaseModel):
    file_path: str = Field(description="Path to insert")
    lineno: int = Field(
        ge=-1,
        description=(
            "Insert after this line (1-based). "
            "0 → beginning of file, -1 → end of file."
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
    start_no: int = Field(
        ge=1,
        description=(
            "Exact first line (1-based) to replace. "
            "Use the line number from the latest read output."
        ),
    )
    end_no: int = Field(
        ge=1,
        description=(
            "Exact last line (1-based) to replace. "
            "Use the line number from the latest read output."
        ),
    )
    prefix: str = Field(
        description=(
            "Substring expected anywhere on the first line to replace. "
            "Use an empty string only when the first line is empty. "
            "Aim for a distinctive snippet."
        ),
    )
    suffix: str = Field(
        description=(
            "Substring expected anywhere on the last line to replace. "
            "Use an empty string only when the last line is empty. "
            "Aim for a distinctive snippet."
        ),
    )
    new_string: str = Field(
        description=(
            "Replacement content. May contain any number of lines. "
            "A trailing newline does not add an extra blank line; "
            "start with a newline only when an intentional blank first line is desired."
        )
    )

    @model_validator(mode="after")
    def _validate_line_order(self) -> "FileReplaceInput":
        if self.end_no < self.start_no:
            raise ValueError("end_no must be greater than or equal to start_no")
        return self


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
        "Insert content into a file. Read the target line first."
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
        "Replace whole lines in a file. "
        "Provide the exact start_no/end_no from the latest read output, "
        "plus prefix/suffix substrings from the first and last lines. "
        "Read the target lines first."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileReplaceInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileReplaceInput.model_validate(args)
        return await _execute_text_replace(
            ctx,
            file_path=inp.file_path,
            start_no=inp.start_no,
            end_no=inp.end_no,
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
    start_no: int,
    end_no: int,
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
    match = _find_text_segment(display.lines, start_no, end_no, prefix, suffix)
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

    tail = original[end_offset:]
    if (new_string == "" or new_string.endswith("\n")) and tail.startswith("\n"):
        tail = tail[1:]
    content = f"{original[:start_offset]}{new_string}{tail}"
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
        metadata={
            "file": file_path,
            "operations": 1,
            "start_line": start_line,
            "end_line": end_line,
        },
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
