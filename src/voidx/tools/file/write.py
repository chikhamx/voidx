from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
from .state import check_read_coverage

from .replace import _apply_resolved_edits, _resolve_edit_target
from .read import _split_display_lines
from .types import ResolvedEdit


class WriteInput(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file")
    op: Literal["insert", "append"] = Field(
        description="Line operation: insert content before a line, or append content to end of file."
    )
    lineno: int | None = Field(
        default=None,
        description="For insert: 0-based line number to insert before. Ignored for append.",
    )
    new_string: str = Field(
        default="",
        description="For insert and append: content to add.",
    )

    @model_validator(mode="after")
    def _validate_write_input(self) -> "WriteInput":
        if self.op == "insert" and self.lineno is None:
            raise ValueError("lineno is required when op=insert")
        return self


class WriteTool(BaseTool):
    id = "write"
    description = "Insert or append whole lines by line number. Read target lines first."

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

    # 0-based insert-before: lineno=N means insert before line (N+1) in 1-based
    # ResolvedEdit("insert", X, X, ...) inserts after line X (1-based)
    # So: insert before line (N+1) = insert after line N = ResolvedEdit("insert", N, N, ...)
    resolved_lineno = inp.lineno
    if inp.lineno > total_lines:
        return ToolResult(
            output=f"Cannot insert before line {inp.lineno + 1}: file has {total_lines} lines.",
            metadata={"error": True},
        )

    # lineno=total_lines is append position (no existing line affected), skip coverage
    if total_lines > 0 and inp.lineno < total_lines:
        coverage_error = check_read_coverage(ctx, path, inp.lineno + 1, inp.lineno + 1, display_path=inp.file_path)
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