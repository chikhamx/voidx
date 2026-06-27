from __future__ import annotations

from dataclasses import asdict

from pydantic import BaseModel, Field, model_validator

from voidx.diffing import make_file_diff, make_structured_diff
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
    _result_trailing_newline,
    _validate_resolved_edits,
)
from .read import _join_display_lines, _split_display_lines, _split_edit_lines
from .types import DisplayLines, ResolvedEdit



class FileReplaceInput(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file")
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
        description="Substring expected anywhere on the first line to replace.",
    )
    suffix: str = Field(
        description="Substring expected anywhere on the last line to replace.",
    )
    new_string: str = Field(
        description="Replacement content. May contain any number of lines.",
    )

    @model_validator(mode="after")
    def _validate_line_order(self) -> "FileReplaceInput":
        if self.end_no < self.start_no:
            raise ValueError("end_no must be greater than or equal to start_no")
        return self


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
        try:
            inp = FileReplaceInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
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

    _, _, start_line, end_line = match
    coverage_error = check_read_coverage(ctx, path, start_line, end_line, display_path=file_path)
    if coverage_error:
        return ToolResult(output=f"Edit 0: {coverage_error}", metadata={"error": True})

    key = str(path.resolve())
    existing_coverage = ctx.file_read_coverage.get(key, {})
    current_fingerprint = asdict(file_fingerprint(path))
    old_ranges = [
        item.copy()
        for item in existing_coverage.get("ranges", [])
    ] if existing_coverage.get("fingerprint") == current_fingerprint else []

    lines = list(display.lines)
    new_lines = _split_edit_lines(new_string)
    lines[start_line - 1:end_line] = new_lines

    # Head-line dedup: if the first line of new_string exactly matches the
    # line immediately before the replaced range, consume that line too.
    actual_start_line = start_line
    if new_lines and new_lines[0] != "":
        prev_idx = start_line - 2
        if prev_idx >= 0 and lines[prev_idx] == new_lines[0]:
            del lines[prev_idx]
            actual_start_line = start_line - 1

    # Tail-line dedup: if the last line of new_string exactly matches the
    # first line after the replaced range, consume that line too.
    actual_end_line = end_line
    if new_lines and new_lines[-1] != "":
        head_shift = 1 if actual_start_line < start_line else 0
        next_idx = start_line - 1 + len(new_lines) - head_shift
        if next_idx < len(lines) and lines[next_idx] == new_lines[-1]:
            del lines[next_idx]
            actual_end_line = end_line + 1

    # Trailing newline: preserve the original file's trailing newline.
    # _split_edit_lines already strips a trailing \n from new_string, so
    # both "foo" and "foo\n" produce the same line list.  The original
    # trailing newline is kept unless the file becomes empty.
    trailing_newline = display.trailing_newline if lines else False
    content = _join_display_lines(lines, trailing_newline=trailing_newline)

    await save_file_version(ctx, path, display_path=file_path, tool_name=tool_name)
    path.write_text(content, encoding="utf-8")
    diff = make_file_diff(file_path, original, content)
    file_diff = make_structured_diff(file_path, original, content)
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
            "start_line": actual_start_line,
            "end_line": actual_end_line,
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
        # Similarly, insert at (total_lines, total_lines) is an append —
        # no existing lines are modified.
        if (edit.start_line, edit.end_line) == (0, 0):
            continue
        if edit.operation == "insert" and edit.start_line == total_lines and edit.end_line == total_lines:
            continue
        coverage_error = check_read_coverage(ctx, path, edit.start_line, edit.end_line, display_path=file_path)
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
    file_diff = make_structured_diff(file_path, original, content)
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
