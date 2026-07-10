from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
from .state import (
    check_staleness,
    clear_file_tracking,
    clear_read_coverage,
    clear_tree_tracking,
    move_file_tracking,
    record_mtime,
    save_file_version,
)


class MoveSpec(BaseModel):
    src: str = Field(description="Source file or directory path for a move operation.")
    dest: str = Field(description="Destination file or directory path for a move operation.")
    overwrite: bool = Field(
        default=False,
        description="Whether this move may replace an existing destination file or directory.",
    )


class ManageInput(BaseModel):
    op: Literal["create", "delete", "move"] = Field(
        description="File or directory lifecycle operation: create, delete, or move/rename."
    )
    kind: Literal["file", "dir"] = Field(
        default="file",
        description="Whether the operation targets a file or directory.",
    )
    paths: str | list[str] | None = Field(
        default=None,
        description="File or directory path(s); paths is required for op=create and op=delete. Ignored for op=move.",
    )
    moves: list[MoveSpec] | None = Field(
        default=None,
        description="Move mappings required for op=move; each item has src, dest, and per-move overwrite. Ignored for op=create/op=delete.",
    )
    overwrite: bool = Field(
        default=False,
        description="For op=create only: replace an existing file after safety checks. Ignored for directory create, delete, and move.",
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


class ManageTool(BaseTool):
    id = "manage"
    description = "Create an empty file or directory, delete files or directories, or move/rename files or directories. No file content is written; use write for content."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(ManageInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        args = _normalize_legacy_manage_args(args)
        try:
            inp = ManageInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        if inp.op == "create":
            result = await _create_files(ctx, inp)
            if inp.kind == "file" and result.metadata.get("succeeded") == 1 and not inp.overwrite:
                paths = _paths_list(inp.paths)
                if paths:
                    result.next_step_hint = (
                        f"Use the write tool to append content to {paths[0]}. "
                        f"Start with write(file_path=\"{paths[0]}\", op=\"append\", new_string=\"...\")."
                    )
            return result
        if inp.op == "delete":
            return await _delete_files(ctx, inp)
        if inp.op == "move":
            return await _move_files(ctx, inp)
        return ToolResult(output=f"Unknown manage operation: {inp.op}", metadata={"error": True})


def _normalize_legacy_manage_args(args: dict) -> dict:
    if not isinstance(args, dict):
        return args
    normalized = dict(args)
    op = normalized.get("op")
    file_path = normalized.get("file_path") or normalized.get("path")
    if op in {"create", "delete"} and file_path and not normalized.get("paths"):
        normalized["paths"] = file_path
    if op == "move" and file_path and normalized.get("dest_path") and not normalized.get("moves"):
        normalized["moves"] = [{
            "src": file_path,
            "dest": normalized["dest_path"],
            "overwrite": bool(normalized.get("overwrite", False)),
        }]
        normalized.pop("paths", None)
    return normalized


def _paths_list(paths: str | list[str] | None) -> list[str]:
    if paths is None:
        return []
    if isinstance(paths, str):
        return [paths]
    return paths


def _batch_result(operation: str, results: list[dict], kind: Literal["file", "dir"] = "file") -> ToolResult:
    success_status = {"create": "created", "delete": "deleted", "move": "moved"}[operation]
    succeeded = sum(1 for item in results if item.get("status") == success_status)
    skipped = sum(1 for item in results if item.get("status") == "skipped")
    failed = sum(1 for item in results if item.get("status") == "error")
    total = len(results)
    verb = {"create": "Created", "delete": "Deleted", "move": "Moved"}[operation]
    noun = "files" if kind == "file" else "directories"
    parts = [f"{verb} {succeeded}/{total} {noun}"]
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{failed} failed")
    summary = ", ".join(parts)
    return ToolResult(
        title=f"{verb} {succeeded}/{total} {noun}",
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
        results.append(await _create_one(ctx, file_path, inp.overwrite, inp.kind, tool_name="manage"))
    return _batch_result("create", results, inp.kind)


async def _delete_files(ctx: ToolContext, inp: ManageInput) -> ToolResult:
    results = []
    for file_path in _paths_list(inp.paths):
        results.append(await _delete_one(ctx, file_path, inp.kind, tool_name="manage"))
    return _batch_result("delete", results, inp.kind)


async def _move_files(ctx: ToolContext, inp: ManageInput) -> ToolResult:
    results = []
    for move in inp.moves or []:
        results.append(await _move_one(ctx, move.src, move.dest, move.overwrite, inp.kind, tool_name="manage"))
    return _batch_result("move", results, inp.kind)


def _lexical_path(ctx: ToolContext, file_path: str) -> Path:
    raw = Path(file_path)
    if file_path.startswith("~") or raw.is_absolute():
        return raw.expanduser()
    return Path(ctx.workspace) / raw


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor:
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def _resolve_directory_path(ctx: ToolContext, file_path: str) -> tuple[Path | None, str | None]:
    lexical = _lexical_path(ctx, file_path)
    normalized = Path(os.path.normpath(str(lexical)))
    if _has_symlink_component(lexical) or _has_symlink_component(normalized):
        return None, f"Directory path contains a symbolic link: {file_path}"
    path = resolve_safe(ctx.workspace, file_path, ctx.sandbox_extra_paths)
    if path is None:
        return None, f"Path traversal blocked: {file_path}"
    return path, None


def _protected_roots(ctx: ToolContext) -> set[Path]:
    roots = {Path(ctx.workspace).resolve()}
    roots.update(Path(path).expanduser().resolve() for path in ctx.sandbox_extra_paths)
    return roots


def _is_protected_root(ctx: ToolContext, path: Path) -> bool:
    return path.resolve() in _protected_roots(ctx)


async def _create_one(
    ctx: ToolContext,
    file_path: str,
    overwrite: bool,
    kind: Literal["file", "dir"],
    *,
    tool_name: str,
) -> dict:
    if kind == "dir":
        return _create_directory(ctx, file_path)

    path = resolve_safe(ctx.workspace, file_path, ctx.sandbox_extra_paths)
    if path is None:
        return {"file": file_path, "status": "error", "reason": f"Path traversal blocked: {file_path}"}
    if path.exists() and path.is_dir():
        return {"file": file_path, "status": "error", "reason": f"Path is a directory: {file_path}"}
    if path.exists() and not overwrite:
        return {"file": file_path, "status": "skipped", "reason": "already exists, set overwrite=True to replace"}
    if path.exists():
        if str(path.resolve()) not in ctx.file_mtimes:
            return {"file": file_path, "status": "error", "reason": f"File must be read before overwrite: {file_path}. Please read the file first."}
        stale = check_staleness(ctx, path)
        if stale:
            return {"file": file_path, "status": "error", "reason": stale}
        await save_file_version(ctx, path, display_path=file_path, tool_name=tool_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    record_mtime(ctx, path)
    clear_read_coverage(ctx, path)
    return {"file": file_path, "status": "created"}


def _create_directory(ctx: ToolContext, file_path: str) -> dict:
    path, error = _resolve_directory_path(ctx, file_path)
    if error:
        return {"file": file_path, "status": "error", "reason": error}
    assert path is not None
    if path.exists() and not path.is_dir():
        return {"file": file_path, "status": "error", "reason": "Path is a file, not a directory"}
    if path.exists():
        return {"file": file_path, "status": "skipped", "reason": "directory already exists"}
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"file": file_path, "status": "error", "reason": str(exc)}
    return {"file": file_path, "status": "created"}


async def _delete_one(
    ctx: ToolContext,
    file_path: str,
    kind: Literal["file", "dir"],
    *,
    tool_name: str,
) -> dict:
    if kind == "dir":
        return _delete_directory(ctx, file_path)

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


def _delete_directory(ctx: ToolContext, file_path: str) -> dict:
    path, error = _resolve_directory_path(ctx, file_path)
    if error:
        return {"file": file_path, "status": "error", "reason": error}
    assert path is not None
    if not path.exists():
        return {"file": file_path, "status": "skipped", "reason": "file does not exist"}
    if _is_protected_root(ctx, path):
        return {"file": file_path, "status": "error", "reason": "Protected root directory cannot be deleted"}
    if not path.is_dir():
        return {"file": file_path, "status": "error", "reason": "Path is not a directory"}
    try:
        shutil.rmtree(path)
    except OSError as exc:
        clear_tree_tracking(ctx, path)
        return {"file": file_path, "status": "error", "reason": str(exc)}
    clear_tree_tracking(ctx, path)
    return {"file": file_path, "status": "deleted"}


async def _move_one(
    ctx: ToolContext,
    src: str,
    dest_path: str,
    overwrite: bool,
    kind: Literal["file", "dir"],
    *,
    tool_name: str,
) -> dict:
    if kind == "dir":
        return _move_directory(ctx, src, dest_path, overwrite)

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


def _move_directory(ctx: ToolContext, src: str, dest_path: str, overwrite: bool) -> dict:
    source, source_error = _resolve_directory_path(ctx, src)
    if source_error:
        return {"file": src, "dest": dest_path, "status": "error", "reason": source_error}
    dest, dest_error = _resolve_directory_path(ctx, dest_path)
    if dest_error:
        return {"file": src, "dest": dest_path, "status": "error", "reason": dest_error}
    assert source is not None and dest is not None
    if not source.exists():
        return {"file": src, "dest": dest_path, "status": "skipped", "reason": "source file does not exist"}
    if _is_protected_root(ctx, source) or _is_protected_root(ctx, dest):
        return {"file": src, "dest": dest_path, "status": "error", "reason": "Protected root directory cannot be moved or replaced"}
    if not source.is_dir():
        return {"file": src, "dest": dest_path, "status": "error", "reason": "Source is not a directory"}
    if source == dest:
        return {"file": src, "dest": dest_path, "status": "error", "reason": "Source and destination are the same file"}
    if dest.is_relative_to(source):
        return {"file": src, "dest": dest_path, "status": "error", "reason": "Destination is inside the source directory"}
    if source.is_relative_to(dest):
        return {"file": src, "dest": dest_path, "status": "error", "reason": "Source and destination directory trees overlap"}
    if dest.exists() and not dest.is_dir():
        return {"file": src, "dest": dest_path, "status": "error", "reason": "Destination is a file, not a directory"}
    if dest.exists() and not overwrite:
        return {"file": src, "dest": dest_path, "status": "skipped", "reason": "destination already exists, set overwrite=True to replace"}

    source_root = source
    dest_root = dest if dest.exists() else None
    try:
        if dest_root is not None:
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
    except OSError as exc:
        clear_tree_tracking(ctx, source_root)
        clear_tree_tracking(ctx, dest)
        return {"file": src, "dest": dest_path, "status": "error", "reason": str(exc)}

    clear_tree_tracking(ctx, source_root)
    clear_tree_tracking(ctx, dest)
    return {"file": src, "dest": dest_path, "status": "moved"}
