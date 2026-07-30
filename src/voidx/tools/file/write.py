from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.diffing import make_file_diff, make_structured_diff, render_numbered_diff
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    keep_tool_args,
    model_to_json_schema,
    _resolve_tool_path_for_access,
)
from .state import (
    check_read_coverage,
    check_staleness,
    clear_file_tracking,
    coverage_ranges_snapshot,
    remap_read_coverage_from_file_diff,
    save_file_version,
)

from .overlap import LineOverlap, resolve_overlap
from .replace import _apply_resolved_edits, _resolve_edit_target
from .read import _split_display_lines, _split_edit_lines
from .types import ResolvedEdit
from .io import safe_read_text as _safe_read_text, safe_write_text as _safe_write_text
from .post_edit import format_after_edit, format_range_from_diff


class WriteInput(BaseModel):
    file_path: str = Field(description="Path to the target text file.")
    op: Literal["insert", "append", "write"] = Field(
        description=(
            "Write mode: insert before a 1-based line number, append to an existing file, "
            "or write complete file content."
        )
    )
    lineno: int | None = Field(
        default=None,
        description="For op=insert, 1-based line number to insert before. Ignored for op=append and op=write.",
    )
    new_string: str = Field(
        default="",
        description=(
            "Text to insert or append; for op=write, complete file content. For op=insert, "
            "up to three non-empty leading and trailing lines that exactly match adjacent "
            "file lines are treated as overlap rather than duplicated."
        ),
    )

    @model_validator(mode="after")
    def _validate_write_input(self) -> "WriteInput":
        if self.op == "insert":
            if self.lineno is None:
                raise ValueError("lineno is required when op=insert")
            if self.lineno < 1:
                raise ValueError("write.insert lineno is 1-based; use lineno=1 to insert at the beginning of the file")
        return self


def _normalize_write_args(args):
    if not isinstance(args, dict):
        return args
    op = str(args.get("op") or "").strip().lower()
    if op == "insert":
        return keep_tool_args(args, {"file_path", "op", "lineno", "new_string"})
    if op in {"append", "write"}:
        return keep_tool_args(args, {"file_path", "op", "new_string"})
    return args


class WriteTool(BaseTool):
    id = "write"
    description = 'Edit text files: write op="insert" before a 1-based line, op="append" at EOF, or op="write" creates or fully overwrites full content.'

    def parameters_schema(self) -> dict:
        return model_to_json_schema(WriteInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        args = _normalize_write_args(args)
        try:
            inp = WriteInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        if inp.op == "insert":
            return await _execute_write_insert(ctx, inp)
        if inp.op == "append":
            return await _execute_write_append(ctx, inp)
        if inp.op == "write":
            return await _execute_write_full(ctx, inp)
        return ToolResult(output=f"Unknown write operation: {inp.op}", metadata={"error": True})


async def _execute_write_insert(ctx: ToolContext, inp: WriteInput) -> ToolResult:
    if inp.new_string == "":
        return ToolResult(
            title="No changes",
            output="Insertion content is empty; no changes applied.",
            summary="No changes",
            metadata={"file": inp.file_path, "operations": 0},
        )
    path, error = await _resolve_tool_path_for_access(
        ctx,
        inp.file_path,
        write=True,
        require_exists=True,
        prompt_label="Write",
        allow_description="Allow this write once",
        deny_description="Do not write this file",
    )
    if error is not None:
        return error
    assert path is not None
    if not path.exists():
        return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})
    original, read_error = _safe_read_text(path)
    if read_error is not None:
        return ToolResult(output=read_error, metadata={"error": True})
    display = _split_display_lines(original)
    total_lines = len(display.lines)
    assert inp.lineno is not None
    if inp.lineno > total_lines + 1:
        return ToolResult(output=f"Cannot insert before line {inp.lineno}: file has {total_lines} lines.", metadata={"error": True})
    resolved_lineno = inp.lineno - 1
    new_lines = _split_edit_lines(inp.new_string)
    overlap = resolve_overlap(
        display.lines[:resolved_lineno],
        new_lines,
        display.lines[resolved_lineno:],
    )

    required_start: int | None = None
    required_end: int | None = None
    if total_lines > 0 and inp.lineno <= total_lines:
        required_start = inp.lineno
        required_end = inp.lineno
    if overlap.head:
        required_start = min(required_start or inp.lineno, inp.lineno - overlap.head)
        required_end = max(required_end or 0, inp.lineno - 1)
    if overlap.tail:
        required_start = min(required_start or inp.lineno, inp.lineno)
        required_end = max(required_end or 0, inp.lineno + overlap.tail - 1)

    if required_start is not None and required_end is not None:
        coverage_error = check_read_coverage(
            ctx,
            path,
            required_start,
            required_end,
            display_path=inp.file_path,
        )
        if coverage_error:
            return ToolResult(
                output=f"{coverage_error}\nRetry after reading lines {required_start}-{required_end}.",
                metadata={"error": True},
            )
    result = await _apply_single_write_edit(
        ctx,
        inp.file_path,
        ResolvedEdit("insert", resolved_lineno, resolved_lineno, inp.new_string),
        resolved_path=path,
        overlap=overlap,
        coverage_checked=True,
    )
    if inp.lineno == total_lines + 1 and total_lines > 0 and overlap == LineOverlap(head=0, tail=0):
        result.next_step_hint = 'Insert at EOF is append; use write op="append" next time.'
    return result


async def _execute_write_append(ctx: ToolContext, inp: WriteInput) -> ToolResult:
    if inp.new_string == "":
        return ToolResult(
            title="No changes",
            output="Append content is empty; no changes applied.",
            summary="No changes",
            metadata={"file": inp.file_path, "operations": 0},
        )
    path, error = await _resolve_tool_path_for_access(
        ctx,
        inp.file_path,
        write=True,
        require_exists=True,
        prompt_label="Write",
        allow_description="Allow this write once",
        deny_description="Do not write this file",
    )
    if error is not None:
        return error
    assert path is not None
    if not path.exists():
        return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})
    original, read_error = _safe_read_text(path)
    if read_error is not None:
        return ToolResult(output=read_error, metadata={"error": True})
    total_lines = len(_split_display_lines(original).lines)
    return await _apply_single_write_edit(
        ctx,
        inp.file_path,
        ResolvedEdit("insert", total_lines, total_lines, inp.new_string),
        resolved_path=path,
    )


async def _execute_write_full(ctx: ToolContext, inp: WriteInput) -> ToolResult:
    path, error = await _resolve_tool_path_for_access(
        ctx,
        inp.file_path,
        write=True,
        allow_missing_write_file=True,
        prompt_label="Write",
        allow_description="Allow this write once",
        deny_description="Do not write this file",
    )
    if error is not None:
        return error
    assert path is not None
    created = not path.exists()
    original = ""
    old_ranges: list[dict] = []
    if path.exists():
        if str(path.resolve()) not in ctx.file_mtimes:
            return ToolResult(
                output=f"File must be read before full overwrite: {inp.file_path}. Please read the file first.",
                metadata={"error": True},
            )
        stale = check_staleness(ctx, path)
        if stale:
            return ToolResult(output=stale, metadata={"error": True})
        original, read_error = _safe_read_text(path)
        if read_error is not None:
            return ToolResult(output=read_error, metadata={"error": True})
        await save_file_version(ctx, path, display_path=inp.file_path, tool_name="write")
        old_ranges = coverage_ranges_snapshot(ctx, path)
    write_error = _safe_write_text(path, inp.new_string)
    if write_error is not None:
        return ToolResult(output=write_error, metadata={"error": True})
    edited_diff = make_structured_diff(inp.file_path, original, inp.new_string)
    formatting = await format_after_edit(
        ctx,
        path,
        display_path=inp.file_path,
        edited_text=inp.new_string,
        format_range=format_range_from_diff(inp.new_string, edited_diff),
    )
    final_text = formatting.final_text
    if final_text is None:
        clear_file_tracking(ctx, path)
        return ToolResult(
            output=f"Write completed, but final file state is unavailable: {formatting.error}",
            metadata={"error": True, "formatting_status": formatting.status},
        )
    diff = make_file_diff(inp.file_path, original, final_text)
    file_diff = make_structured_diff(inp.file_path, original, final_text)
    remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)
    numbered_diff = render_numbered_diff(file_diff)
    title = "File created" if created else "File overwritten"
    output = f"{title}: {inp.file_path}"
    if numbered_diff:
        output = f"{output}\n{numbered_diff}"
    return ToolResult(
        title=title,
        output=output,
        summary=title,
        metadata={
            "file": inp.file_path,
            "operation": "write",
            "created": created,
            "operations": 1,
            "formatting_status": formatting.status,
        },
        diff=diff or None,
    )


async def _apply_single_write_edit(
    ctx: ToolContext,
    file_path: str,
    edit: ResolvedEdit,
    *,
    resolved_path=None,
    overlap: LineOverlap | None = None,
    coverage_checked: bool = False,
) -> ToolResult:
    if resolved_path is None:
        path, error = await _resolve_edit_target(ctx, file_path)
        if error is not None:
            return error
        assert path is not None
    else:
        path = resolved_path
        stale = check_staleness(ctx, path)
        if stale:
            return ToolResult(output=stale, metadata={"error": True})
    original, read_error = _safe_read_text(path)
    if read_error is not None:
        return ToolResult(output=read_error, metadata={"error": True})
    display = _split_display_lines(original)
    return await _apply_resolved_edits(
        ctx,
        path=path,
        file_path=file_path,
        edits=[edit],
        original=original,
        display=display,
        tool_name="write",
        hints=[],
        overlap=overlap,
        coverage_checked=coverage_checked,
    )
