from __future__ import annotations

import shutil
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
from .state import (
    check_staleness,
    clear_file_tracking,
    clear_read_coverage,
    move_file_tracking,
    record_mtime,
    save_file_version,
)


class MoveSpec(BaseModel):
    src: str = Field(description="Source file path")
    dest: str = Field(description="Destination file path")
    overwrite: bool = Field(
        default=False,
        description="Whether to overwrite destination when it already exists.",
    )


class ManageInput(BaseModel):
    op: Literal["create", "delete", "move"] = Field(
        description=(
            "File lifecycle operation: create (create empty file + parent dirs), "
            "delete (delete files), move (move/rename files)"
        )
    )
    paths: str | list[str] | None = Field(
        default=None,
        description=(
            "File path or file paths. Required for op=create/delete. "
            "Use a string for one file, or an array for multiple files. Ignored for op=move."
        ),
    )
    moves: list[MoveSpec] | None = Field(
        default=None,
        description=(
            "Move mappings for op=move. Each item contains src, dest, and optional overwrite. "
            "Ignored for op=create/delete."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "For create: overwrite existing files. For delete: ignored. "
            "For move: ignored; use per-item moves[].overwrite."
        ),
    )

    @model_validator(mode="after")
    def _validate_op_params(self) -> "ManageInput":
        if self.op in ("create", "delete"):
            if not self.paths:
                raise ValueError("paths is required when op=create or op=delete; use paths='a.py' or paths=['a.py', 'b.py']")
            if self.moves:
                raise ValueError("moves is ignored when op=create or op=delete; use paths instead")
        if self.op == "move":
            if not self.moves:
                raise ValueError("moves is required when op=move; use moves=[{'src': 'old.py', 'dest': 'new.py'}]")
            if self.paths:
                raise ValueError("paths is ignored when op=move; use moves instead")
        return self


class FileInput(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file")
    op: Literal["create", "delete", "move"] = Field(
        description=(
            "Deprecated file operation: create (create empty file + parent dirs), "
            "delete (delete file), move (move/rename file). Use manage instead."
        )
    )
    dest_path: str | None = Field(
        default=None,
        description="Destination path for move operation. Required when op=move; ignored for create and delete.",
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


class ManageTool(BaseTool):
    id = "manage"
    description = "Create, delete, or move one or more files."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(ManageInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = ManageInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        if inp.op == "create":
            return await _create_files(ctx, inp)
        if inp.op == "delete":
            return await _delete_files(ctx, inp)
        if inp.op == "move":
            return await _move_files(ctx, inp)
        return ToolResult(output=f"Unknown manage operation: {inp.op}", metadata={"error": True})


class FileTool(BaseTool):
    id = "file"
    description = "Deprecated create/delete/move file lifecycle wrapper. Use manage for file lifecycle operations."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = FileInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        if inp.op == "move":
            new_args = {
                "op": "move",
                "moves": [{"src": inp.file_path, "dest": inp.dest_path, "overwrite": inp.overwrite}],
            }
        else:
            new_args = {"op": inp.op, "paths": inp.file_path, "overwrite": inp.overwrite}
        result = await ManageTool().execute(new_args, ctx)
        result.metadata.update({
            "deprecated_tool": "file",
            "replacement_tool": "manage",
            "remove_after": "one minor release or 30 days",
        })
        if inp.op == "create" and result.metadata.get("succeeded") == 1 and not inp.overwrite:
            result.next_step_hint = (
                f"Use the write tool to append content to {inp.file_path}. "
                f"Start with write(file_path=\"{inp.file_path}\", op=\"append\", new_string=\"...\")."
            )
        return result


def _paths_list(paths: str | list[str] | None) -> list[str]:
    if paths is None:
        return []
    if isinstance(paths, str):
        return [paths]
    return paths


def _batch_result(operation: str, results: list[dict]) -> ToolResult:
    success_status = {"create": "created", "delete": "deleted", "move": "moved"}[operation]
    succeeded = sum(1 for item in results if item.get("status") == success_status)
    skipped = sum(1 for item in results if item.get("status") == "skipped")
    failed = sum(1 for item in results if item.get("status") == "error")
    total = len(results)
    verb = {"create": "Created", "delete": "Deleted", "move": "Moved"}[operation]
    parts = [f"{verb} {succeeded}/{total} files"]
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{failed} failed")
    summary = ", ".join(parts)
    return ToolResult(
        title=f"{verb} {succeeded}/{total} files",
        output=f"{summary}.",
        summary=summary,
        metadata={
            "operation": operation,
            "total": total,
            "succeeded": succeeded,
            "skipped": skipped,
            "failed": failed,
            "results": results,
        },
    )


async def _create_files(ctx: ToolContext, inp: ManageInput) -> ToolResult:
    results = []
    for file_path in _paths_list(inp.paths):
        results.append(await _create_one(ctx, file_path, inp.overwrite, tool_name="manage"))
    return _batch_result("create", results)


async def _delete_files(ctx: ToolContext, inp: ManageInput) -> ToolResult:
    results = []
    for file_path in _paths_list(inp.paths):
        results.append(await _delete_one(ctx, file_path, tool_name="manage"))
    return _batch_result("delete", results)


async def _move_files(ctx: ToolContext, inp: ManageInput) -> ToolResult:
    results = []
    for move in inp.moves or []:
        results.append(await _move_one(ctx, move.src, move.dest, move.overwrite, tool_name="manage"))
    return _batch_result("move", results)


async def _create_one(ctx: ToolContext, file_path: str, overwrite: bool, *, tool_name: str) -> dict:
    path = resolve_safe(ctx.workspace, file_path, ctx.sandbox_extra_paths)
    if path is None:
        return {"file": file_path, "status": "error", "reason": f"Path traversal blocked: {file_path}"}
    if path.exists() and path.is_dir():
        return {"file": file_path, "status": "error", "reason": f"Path is a directory: {file_path}"}
    if path.exists() and not overwrite:
        return {"file": file_path, "status": "skipped", "reason": "already exists, set overwrite=True to replace"}
    if path.exists():
        stale = check_staleness(ctx, path)
        if stale:
            return {"file": file_path, "status": "error", "reason": stale}
        await save_file_version(ctx, path, display_path=file_path, tool_name=tool_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    record_mtime(ctx, path)
    clear_read_coverage(ctx, path)
    return {"file": file_path, "status": "created"}


async def _delete_one(ctx: ToolContext, file_path: str, *, tool_name: str) -> dict:
    path = resolve_safe(ctx.workspace, file_path, ctx.sandbox_extra_paths)
    if path is None:
        return {"file": file_path, "status": "error", "reason": f"Path traversal blocked: {file_path}"}
    if not path.exists():
        return {"file": file_path, "status": "skipped", "reason": "file does not exist"}
    if path.is_dir():
        return {"file": file_path, "status": "error", "reason": f"Path is a directory: {file_path}"}
    stale = check_staleness(ctx, path)
    if stale:
        return {"file": file_path, "status": "error", "reason": stale}
    await save_file_version(ctx, path, display_path=file_path, tool_name=tool_name)
    path.unlink()
    clear_file_tracking(ctx, path)
    return {"file": file_path, "status": "deleted"}


async def _move_one(ctx: ToolContext, src: str, dest_path: str, overwrite: bool, *, tool_name: str) -> dict:
    source = resolve_safe(ctx.workspace, src, ctx.sandbox_extra_paths)
    dest = resolve_safe(ctx.workspace, dest_path, ctx.sandbox_extra_paths)
    if source is None:
        return {"file": src, "dest": dest_path, "status": "error", "reason": f"Path traversal blocked: {src}"}
    if dest is None:
        return {"file": src, "dest": dest_path, "status": "error", "reason": f"Path traversal blocked: {dest_path}"}
    if source == dest:
        return {"file": src, "dest": dest_path, "status": "error", "reason": "Source and destination are the same file"}
    if not source.exists():
        return {"file": src, "dest": dest_path, "status": "skipped", "reason": "source file does not exist"}
    if source.is_dir():
        return {"file": src, "dest": dest_path, "status": "error", "reason": f"Path is a directory: {src}"}
    if dest.exists() and dest.is_dir():
        return {"file": src, "dest": dest_path, "status": "error", "reason": f"Destination is a directory: {dest_path}"}
    source_stale = check_staleness(ctx, source)
    if source_stale:
        return {"file": src, "dest": dest_path, "status": "error", "reason": source_stale}
    if dest.exists() and not overwrite:
        return {"file": src, "dest": dest_path, "status": "skipped", "reason": "destination already exists, set overwrite=True to replace"}
    if dest.exists():
        dest_stale = check_staleness(ctx, dest)
        if dest_stale:
            return {"file": src, "dest": dest_path, "status": "error", "reason": dest_stale}
        await save_file_version(ctx, dest, display_path=dest_path, tool_name=tool_name)
    await save_file_version(ctx, source, display_path=src, tool_name=tool_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    move_file_tracking(ctx, source, dest)
    return {"file": src, "dest": dest_path, "status": "moved"}


