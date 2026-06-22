from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
from voidx.tools.file_state import check_read_coverage

from .edit_execute import _apply_resolved_edits, _resolve_edit_target
from .read import _split_display_lines
from .types import ResolvedEdit


class LineInput(BaseModel):
    file_path: str = Field(description="Path to the file")
    op: Literal["insert", "delete"] = Field(
        description="Line operation: insert content at lineno or delete lines at lineno."
    )
    lineno: int = Field(
        ge=-1,
        description=(
            "Line number (1-based). For insert: insert after this line "
            "(0 means beginning of file, -1 means end of file). "
            "For delete: first line to delete."
        ),
    )
    end_no: int | None = Field(
        default=None,
        ge=1,
        description=(
            "For delete only: last line to delete (1-based). "
            "If omitted, deletes only the lineno line."
        ),
    )
    new_string: str = Field(
        default="",
        description=(
            "For insert only: content to insert. A trailing newline does not add "
            "an extra blank line."
        ),
    )

    @model_validator(mode="after")
    def _validate_line_input(self) -> "LineInput":
        if self.op == "delete":
            if self.lineno < 1:
                raise ValueError("lineno must be at least 1 when op=delete")
            if self.end_no is not None and self.end_no < self.lineno:
                raise ValueError("end_no must be greater than or equal to lineno")
        return self


class LineTool(BaseTool):
    id = "line"
    description = "Insert or delete whole lines by line number. Read target lines first."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LineInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = LineInput.model_validate(args)
        if inp.op == "insert":
            return await _execute_line_insert(ctx, inp)
        if inp.op == "delete":
            return await _execute_line_delete(ctx, inp)
        return ToolResult(output=f"Unknown line operation: {inp.op}", metadata={"error": True})


async def _execute_line_insert(ctx: ToolContext, inp: LineInput) -> ToolResult:
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
    resolved_lineno = inp.lineno
    if inp.lineno == -1:
        resolved_lineno = total_lines
    elif inp.lineno > total_lines:
        return ToolResult(
            output=f"Cannot insert after line {inp.lineno}: file has {total_lines} lines.",
            metadata={"error": True},
        )

    if total_lines > 0 and resolved_lineno == 0:
        coverage_error = check_read_coverage(ctx, path, 1, 1)
        if coverage_error:
            return ToolResult(output=coverage_error, metadata={"error": True})

    return await _apply_single_line_edit(
        ctx,
        inp.file_path,
        ResolvedEdit("insert", resolved_lineno, resolved_lineno, inp.new_string),
    )


async def _execute_line_delete(ctx: ToolContext, inp: LineInput) -> ToolResult:
    path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
    if path is None:
        return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
    if not path.exists():
        return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})
    total_lines = len(_split_display_lines(path.read_text(encoding="utf-8", errors="replace")).lines)
    end_no = inp.end_no or inp.lineno
    if inp.lineno > total_lines or end_no > total_lines:
        return ToolResult(
            output=f"Lines {inp.lineno}-{end_no} out of range (file has {total_lines} lines).",
            metadata={"error": True},
        )

    result = await _apply_single_line_edit(
        ctx,
        inp.file_path,
        ResolvedEdit("replace", inp.lineno, end_no, ""),
    )
    if result.metadata.get("error") is not True:
        result.metadata["start_line"] = inp.lineno
        result.metadata["end_line"] = end_no
    return result


async def _apply_single_line_edit(ctx: ToolContext, file_path: str, edit: ResolvedEdit) -> ToolResult:
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
        tool_name="line",
        hints=[],
    )
