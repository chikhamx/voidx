from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.diffing import make_file_diff, make_structured_diff, render_numbered_diff
from voidx.tools.base import BaseTool, ToolContext, ToolResult, _resolve_tool_path_for_access, model_to_json_schema
from .state import check_read_coverage, check_staleness, clear_read_coverage, record_mtime, save_file_version

from .replace import _apply_resolved_edits, _resolve_edit_target
from .read import _split_display_lines
from .types import ResolvedEdit
from .safe_path import SafePathExecutor


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
        description="Text to insert or append; for op=write, complete file content.",
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
    description = 'Edit text files: write op="insert" before a 1-based line, op="append" at EOF, or op="write" to create/overwrite full content.'

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
    assert inp.lineno is not None
    if inp.lineno > total_lines + 1:
        return ToolResult(output=f"Cannot insert before line {inp.lineno}: file has {total_lines} lines.", metadata={"error": True})
    resolved_lineno = inp.lineno - 1
    if total_lines > 0 and inp.lineno <= total_lines:
        coverage_error = check_read_coverage(ctx, path, inp.lineno, inp.lineno, display_path=inp.file_path)
        if coverage_error:
            return ToolResult(
                output=f"{coverage_error}\nRetry after reading lines {inp.lineno}-{inp.lineno}.",
                metadata={"error": True},
            )
    result = await _apply_single_write_edit(
        ctx,
        inp.file_path,
        ResolvedEdit("insert", resolved_lineno, resolved_lineno, inp.new_string),
        resolved_path=path,
    )
    if inp.lineno == total_lines + 1 and total_lines > 0:
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
    write_error = _safe_write_text(path, inp.new_string)
    if write_error is not None:
        return ToolResult(output=write_error, metadata={"error": True})
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


async def _apply_single_write_edit(
    ctx: ToolContext,
    file_path: str,
    edit: ResolvedEdit,
    *,
    resolved_path=None,
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
    )


def _safe_read_text(path: Path) -> tuple[str, str | None]:
    executor = SafePathExecutor()
    try:
        authorized = executor.authorize_existing(path, access="read")
    except OSError as exc:
        return "", str(exc)
    result = executor.read_text(authorized, encoding="utf-8", errors="replace")
    if not result.ok:
        return "", result.error
    assert isinstance(result.value, str)
    return result.value, None


def _safe_write_text(path: Path, content: str) -> str | None:
    executor = SafePathExecutor()
    authorized = executor.authorize_target(path, access="write")
    result = executor.write_text(authorized, content, encoding="utf-8")
    if not result.ok:
        return result.error
    return None
