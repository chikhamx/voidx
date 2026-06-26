from __future__ import annotations

import shutil
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.diffing import make_file_diff
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
from voidx.tools.file_state import (
    check_staleness,
    clear_file_tracking,
    clear_read_coverage,
    move_file_tracking,
    record_mtime,
    save_file_version,
)


class FileInput(BaseModel):
    file_path: str = Field(description="Path to the file")
    op: Literal["create", "delete", "move"] = Field(
        description=(
            "File operation: create (create empty file + parent dirs), "
            "delete (delete file), move (move/rename file)"
        )
    )
    dest_path: str | None = Field(
        default=None,
        description="Destination path for move operation. Required when op=move.",
    )
    overwrite: bool = Field(
        default=False,
        description="For create: overwrite if file exists. For move: overwrite destination.",
    )

    @model_validator(mode="after")
    def _validate_dest_path(self) -> "FileInput":
        if self.op == "move" and not self.dest_path:
            raise ValueError("dest_path is required when op=move")
        return self


class FileTool(BaseTool):
    id = "file"
    description = "Manage files: create empty files, delete files, or move/rename files."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = FileInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        if inp.op == "create":
            return await _create_file(ctx, inp)
        if inp.op == "delete":
            return await _delete_file(ctx, inp)
        if inp.op == "move":
            return await _move_file(ctx, inp)
        return ToolResult(output=f"Unknown file operation: {inp.op}", metadata={"error": True})


async def _create_file(ctx: ToolContext, inp: FileInput) -> ToolResult:
    path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
    if path is None:
        return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
    if path.exists() and path.is_dir():
        return ToolResult(output=f"Path is a directory: {inp.file_path}", metadata={"error": True})
    if path.exists() and not inp.overwrite:
        return ToolResult(
            output=f"File already exists: {inp.file_path}. Read it first or set overwrite=True.",
            metadata={"error": True},
        )

    old_content = ""
    overwritten = path.exists()
    if overwritten:
        stale = check_staleness(ctx, path)
        if stale:
            return ToolResult(output=stale, metadata={"error": True})
        old_content = path.read_text(encoding="utf-8", errors="replace")
        await save_file_version(ctx, path, display_path=inp.file_path, tool_name="file")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    record_mtime(ctx, path)
    clear_read_coverage(ctx, path)

    diff = make_file_diff(inp.file_path, old_content, "") if overwritten and old_content else ""
    title = "File overwritten" if overwritten else "File created"
    hint = ""
    if not overwritten:
        hint = (
            f"Use the write tool to append content to {inp.file_path} in batches of up to 30 lines. "
            f"Start with write(file_path=\"{inp.file_path}\", op=\"append\", new_string=\"...\")."
        )
    return ToolResult(
        title=title,
        output=f"{title}: {inp.file_path}",
        summary=title,
        metadata={"file": inp.file_path, "operation": "create", "overwritten": overwritten},
        diff=diff or None,
        next_step_hint=hint,
    )


async def _delete_file(ctx: ToolContext, inp: FileInput) -> ToolResult:
    path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
    if path is None:
        return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
    if not path.exists():
        return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})
    if path.is_dir():
        return ToolResult(output=f"Path is a directory: {inp.file_path}", metadata={"error": True})

    stale = check_staleness(ctx, path)
    if stale:
        return ToolResult(output=stale, metadata={"error": True})

    old_content = path.read_text(encoding="utf-8", errors="replace")
    await save_file_version(ctx, path, display_path=inp.file_path, tool_name="file")
    path.unlink()
    clear_file_tracking(ctx, path)
    diff = make_file_diff(
        inp.file_path,
        old_content,
        "",
        old_label=f"a/{inp.file_path}",
        new_label="/dev/null",
    )
    return ToolResult(
        title="File deleted",
        output=f"File deleted: {inp.file_path}",
        summary="File deleted",
        metadata={"file": inp.file_path, "operation": "delete"},
        diff=diff or None,
    )


async def _move_file(ctx: ToolContext, inp: FileInput) -> ToolResult:
    source = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
    dest = resolve_safe(ctx.workspace, inp.dest_path or "", ctx.sandbox_extra_paths)
    if source is None:
        return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
    if dest is None:
        return ToolResult(output=f"Path traversal blocked: {inp.dest_path}", metadata={"error": True})
    if source == dest:
        return ToolResult(output="Source and destination are the same file.", metadata={"error": True})
    if not source.exists():
        return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})
    if source.is_dir():
        return ToolResult(output=f"Path is a directory: {inp.file_path}", metadata={"error": True})
    if dest.exists() and dest.is_dir():
        return ToolResult(output=f"Destination is a directory: {inp.dest_path}", metadata={"error": True})
    if dest.exists() and not inp.overwrite:
        return ToolResult(
            output=f"Destination already exists: {inp.dest_path}. Set overwrite=True to replace it.",
            metadata={"error": True},
        )

    source_stale = check_staleness(ctx, source)
    if source_stale:
        return ToolResult(output=source_stale, metadata={"error": True})
    if dest.exists():
        dest_stale = check_staleness(ctx, dest)
        if dest_stale:
            return ToolResult(output=dest_stale, metadata={"error": True})

    await save_file_version(ctx, source, display_path=inp.file_path, tool_name="file")
    if dest.exists():
        await save_file_version(ctx, dest, display_path=inp.dest_path, tool_name="file")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    move_file_tracking(ctx, source, dest)
    return ToolResult(
        title="File moved",
        output=f"File moved: {inp.file_path} -> {inp.dest_path}",
        summary="File moved",
        metadata={
            "file": inp.file_path,
            "dest_file": inp.dest_path,
            "operation": "move",
            "overwritten": inp.overwrite,
        },
    )
