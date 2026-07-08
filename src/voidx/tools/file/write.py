from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.diffing import make_file_diff, make_structured_diff, render_numbered_diff
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
from .state import check_read_coverage, check_staleness, clear_read_coverage, record_mtime, save_file_version

from .replace import _apply_resolved_edits, _resolve_edit_target
from .read import _split_display_lines
from .types import ResolvedEdit


class WriteInput(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file")
    op: Literal["insert", "append", "write"] = Field(
        description="Write mode: insert before a 1-based line number, append to end of file, or write full file content."
    )
    lineno: int | None = Field(
        default=None,
        description="For insert: 1-based line number to insert before. Ignored for append and write.",
    )
    new_string: str = Field(
        default="",
        description="Content to add or full replacement content for op=write.",
    )

    @model_validator(mode="after")
    def _validate_write_input(self) -> "WriteInput":
        if self.op == "insert":
            if self.lineno is None:
                raise ValueError("lineno is required when op=insert")
            if self.lineno < 1:
                raise ValueError("write.insert lineno is 1-based; use lineno=1 to insert at the beginning of the file")
        return self


class WriteTool(BaseTool):
    id = "write"
    description = "Insert, append, or fully overwrite file content. Insert line numbers are 1-based."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(WriteInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
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
    path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
    if path is None:
        return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
    if not path.exists():
        return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})
    original = path.read_text(encoding="utf-8", errors="replace")
    total_lines = len(_split_display_lines(original).lines)
    assert inp.lineno is not None
    if inp.lineno > total_lines + 1:
        return ToolResult(output=f"Cannot insert before line {inp.lineno}: file has {total_lines} lines.", metadata={"error": True})
    resolved_lineno = inp.lineno - 1
    if total_lines > 0 and inp.lineno <= total_lines:
        coverage_error = check_read_coverage(ctx, path, inp.lineno, inp.lineno, display_path=inp.file_path)
        if coverage_error:
            return ToolResult(output=coverage_error, metadata={"error": True})
    return await _apply_single_write_edit(
        ctx,
        inp.file_path,
        ResolvedEdit("insert", resolved_lineno, resolved_lineno, inp.new_string),
    )


async def _execute_write_append(ctx: ToolContext, inp: WriteInput) -> ToolResult:
    if inp.new_string == "":
        return ToolResult(
            title="No changes",
            output="Append content is empty; no changes applied.",
            summary="No changes",
            metadata={"file": inp.file_path, "operations": 0},
        )
    path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
    if path is None:
        return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
    if not path.exists():
        return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})
    original = path.read_text(encoding="utf-8", errors="replace")
    total_lines = len(_split_display_lines(original).lines)
    return await _apply_single_write_edit(
        ctx,
        inp.file_path,
        ResolvedEdit("insert", total_lines, total_lines, inp.new_string),
    )


async def _execute_write_full(ctx: ToolContext, inp: WriteInput) -> ToolResult:
    path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
    if path is None:
        return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
    created = not path.exists()
    original = ""
    if path.exists():
        if str(path.resolve()) not in ctx.file_mtimes:
            return ToolResult(
                output=f"File must be read before full overwrite: {inp.file_path}. Please read the file first.",
                metadata={"error": True},
            )
        stale = check_staleness(ctx, path)
        if stale:
            return ToolResult(output=stale, metadata={"error": True})
        original = path.read_text(encoding="utf-8", errors="replace")
        await save_file_version(ctx, path, display_path=inp.file_path, tool_name="write")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(inp.new_string, encoding="utf-8")
    record_mtime(ctx, path)
    clear_read_coverage(ctx, path)
    diff = make_file_diff(inp.file_path, original, inp.new_string)
    file_diff = make_structured_diff(inp.file_path, original, inp.new_string)
    numbered_diff = render_numbered_diff(file_diff)
    title = "File created" if created else "File overwritten"
    output = f"{title}: {inp.file_path}"
    if numbered_diff:
        output = f"{output}\n{numbered_diff}"
    return ToolResult(
        title=title,
        output=output,
        summary=title,
        metadata={"file": inp.file_path, "operation": "write", "created": created, "operations": 1},
        diff=diff or None,
    )


async def _apply_single_write_edit(ctx: ToolContext, file_path: str, edit: ResolvedEdit) -> ToolResult:
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
        edits=[edit],
        original=original,
        display=display,
        tool_name="write",
        hints=[],
    )