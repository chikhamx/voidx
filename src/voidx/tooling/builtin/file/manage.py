from __future__ import annotations

import os
import errno
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.tooling.policy.filesystem.grants import resolve_access
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.arguments import (
    drop_nullish_tool_fields,
    keep_tool_args,
)
from voidx.tooling.domain.schema import model_to_json_schema
from voidx.tooling.application.authorization import authorized_path as _resolve_tool_path_for_access
from voidx.tooling.adapters.persistence.file_snapshot import save_file_version
from voidx.tooling.application.file_state import (
    check_staleness,
    clear_file_tracking,
    clear_read_coverage,
    clear_tree_tracking,
    move_file_tracking,
    record_mtime,
)
from voidx.tooling.policy.filesystem.safe_path import SafePathExecutor


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


def _normalize_manage_args(args):
    if not isinstance(args, dict):
        return args
    op = str(args.get("op") or "").strip().lower()
    if op == "create":
        return drop_nullish_tool_fields(
            keep_tool_args(args, {"op", "kind", "paths", "overwrite"}), "kind"
        )
    if op == "delete":
        return drop_nullish_tool_fields(
            keep_tool_args(args, {"op", "kind", "paths"}), "kind"
        )
    if op == "move":
        return drop_nullish_tool_fields(
            keep_tool_args(args, {"op", "kind", "moves"}), "kind"
        )
    return args


class ManageTool:
    id = "manage"
    description = "Create empty files or directories; create an empty file or directory, delete files or directories, or move/rename paths. No file content is written; use write to add file content."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(ManageInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        args = _normalize_legacy_manage_args(args)
        args = _normalize_manage_args(args)
        try:
            inp = ManageInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        if inp.op == "create":
            result = await _create_files(ctx, inp)
            if inp.kind == "file" and result.metadata.get("succeeded") == 1 and not inp.overwrite:
                paths = _paths_list(inp.paths)
                if paths:
                    result.next_step_hint = f'Created file {paths[0]}. Use write op="append" to add content.'
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
            "error": failed == total and total > 0,
            "results": results,
        }
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


async def _resolve_manage_path(
    ctx: ToolContext,
    file_path: str,
    *,
    require_exists: bool,
    allow_missing_write_file: bool = False,
    object_type: Literal["file", "dir"] = "file",
) -> tuple[Path | None, str | None]:
    if ctx.authorization_service.access_grants is not None:
        resolution = resolve_access(
            ctx.workspace,
            file_path,
            access="write",
            access_grants=ctx.authorization_service.access_grants(),
            require_exists=require_exists,
            allow_missing_write_file=allow_missing_write_file,
            object_type=object_type,
        )
        if resolution.action == "allow" and resolution.intent is not None:
            return resolution.intent.normalized_path, None
        if resolution.action == "deny":
            return None, resolution.reason
    path, error = await _resolve_tool_path_for_access(
        ctx,
        file_path,
        write=True,
        require_exists=require_exists,
        allow_missing_write_file=allow_missing_write_file,
        object_type=object_type,
        prompt_label="Write",
        allow_description="Allow this write once",
        deny_description="Do not write this file",
    )
    if error is not None:
        return None, error.output
    assert path is not None
    return path, None


async def _resolve_directory_path(ctx: ToolContext, file_path: str, *, require_exists: bool = True) -> tuple[Path | None, str | None]:
    lexical = _lexical_path(ctx, file_path)
    normalized = Path(os.path.normpath(str(lexical)))
    if _has_symlink_component(lexical) or _has_symlink_component(normalized):
        return None, f"Directory path contains a symbolic link: {file_path}"
    return await _resolve_manage_path(
        ctx,
        file_path,
        require_exists=require_exists,
        allow_missing_write_file=True,
        object_type="dir",
    )


def _protected_roots(ctx: ToolContext) -> set[Path]:
    roots = {Path(ctx.workspace).resolve()}
    for path in [*ctx.authorization_service.write_files, *ctx.authorization_service.write_dirs]:
        try:
            roots.add(Path(path).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            continue
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
        return await _create_directory(ctx, file_path)

    path, error = await _resolve_manage_path(ctx, file_path, require_exists=False, allow_missing_write_file=True)
    if error:
        return {"file": file_path, "status": "error", "reason": error}
    assert path is not None
    created = not path.exists()
    if path.exists() and path.is_dir():
        return {"file": file_path, "status": "error", "reason": f"Path is a directory: {file_path}"}
    if path.exists() and not overwrite:
        return {"file": file_path, "status": "skipped", "reason": "already exists, set overwrite=True to replace"}
    if path.exists():
        if str(path.resolve()) not in ctx.file_state.mtimes:
            return {"file": file_path, "status": "error", "reason": f"File must be read before overwrite: {file_path}. Please read the file first."}
        stale = check_staleness(ctx, path)
        if stale:
            return {"file": file_path, "status": "error", "reason": stale}
        await save_file_version(ctx, path, display_path=file_path, tool_name=tool_name)
    executor = SafePathExecutor()
    authorized = executor.authorize_target(path, access="write")
    write_result = executor.write_text(authorized, "")
    if not write_result.ok:
        return {"file": file_path, "status": "error", "reason": write_result.error}
    record_mtime(ctx, path)
    clear_read_coverage(ctx, path)
    if created:
        await ctx.authorization_service.record_created_path(
            ctx.workspace,
            path,
            object_type="file",
        )
    return {"file": file_path, "status": "created"}


async def _create_directory(ctx: ToolContext, file_path: str) -> dict:
    path, error = await _resolve_directory_path(ctx, file_path, require_exists=False)
    if error:
        return {"file": file_path, "status": "error", "reason": error}
    assert path is not None
    if path.exists() and not path.is_dir():
        return {"file": file_path, "status": "error", "reason": "Path is a file, not a directory"}
    if path.exists():
        return {"file": file_path, "status": "skipped", "reason": "directory already exists"}
    executor = SafePathExecutor()
    authorized = executor.authorize_target(path, access="write")
    create_result = executor.create_dir(authorized)
    if not create_result.ok:
        return {"file": file_path, "status": "error", "reason": create_result.error}
    await ctx.authorization_service.record_created_path(
        ctx.workspace,
        path,
        object_type="dir",
    )
    return {"file": file_path, "status": "created"}


async def _delete_one(
    ctx: ToolContext,
    file_path: str,
    kind: Literal["file", "dir"],
    *,
    tool_name: str,
) -> dict:
    if kind == "dir":
        return await _delete_directory(ctx, file_path)

    path, error = await _resolve_manage_path(ctx, file_path, require_exists=True)
    if error:
        return {"file": file_path, "status": "error", "reason": error}
    assert path is not None
    if not path.exists():
        return {"file": file_path, "status": "skipped", "reason": "file does not exist"}
    if path.is_dir():
        return {"file": file_path, "status": "error", "reason": f"Path is a directory: {file_path}"}
    stale = check_staleness(ctx, path)
    if stale:
        return {"file": file_path, "status": "error", "reason": stale}
    await save_file_version(ctx, path, display_path=file_path, tool_name=tool_name)
    executor = SafePathExecutor()
    authorized = executor.authorize_existing(path, access="write")
    delete_result = executor.delete_file(authorized)
    if not delete_result.ok:
        return {"file": file_path, "status": "error", "reason": delete_result.error}
    clear_file_tracking(ctx, path)
    await ctx.authorization_service.forget_created_path(
        ctx.workspace,
        path,
        object_type="file",
    )
    return {"file": file_path, "status": "deleted"}


async def _delete_directory(ctx: ToolContext, file_path: str) -> dict:
    path, error = await _resolve_directory_path(ctx, file_path)
    if error:
        return {"file": file_path, "status": "error", "reason": error}
    assert path is not None
    if not path.exists():
        return {"file": file_path, "status": "skipped", "reason": "file does not exist"}
    if _is_protected_root(ctx, path):
        return {"file": file_path, "status": "error", "reason": "Protected root directory cannot be deleted"}
    if not path.is_dir():
        return {"file": file_path, "status": "error", "reason": "Path is not a directory"}
    executor = SafePathExecutor()
    authorized = executor.authorize_existing(path, access="write")
    delete_result = executor.delete_tree(authorized)
    if not delete_result.ok:
        clear_tree_tracking(ctx, path)
        return {"file": file_path, "status": "error", "reason": delete_result.error}
    clear_tree_tracking(ctx, path)
    await ctx.authorization_service.forget_created_path(
        ctx.workspace,
        path,
        object_type="dir",
    )
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
        return await _move_directory(ctx, src, dest_path, overwrite)

    source, source_error = await _resolve_manage_path(ctx, src, require_exists=True)
    if source_error:
        return {"file": src, "dest": dest_path, "status": "error", "reason": source_error}
    dest, dest_error = await _resolve_manage_path(ctx, dest_path, require_exists=False, allow_missing_write_file=True)
    if dest_error:
        return {"file": src, "dest": dest_path, "status": "error", "reason": dest_error}
    assert source is not None and dest is not None
    if source == dest:
        return {"file": src, "dest": dest_path, "status": "error", "reason": "Source and destination are the same file"}
    if not source.exists():
        return {"file": src, "dest": dest_path, "status": "skipped", "reason": "source file does not exist"}
    if source.is_dir():
        return {"file": src, "dest": dest_path, "status": "error", "reason": f"Path is a directory: {src}"}
    destination_created = not dest.exists()
    if dest.exists() and dest.is_dir():
        return {"file": src, "dest": dest_path, "status": "error", "reason": f"Destination is a directory: {dest_path}"}
    source_stale = check_staleness(ctx, source)
    if source_stale:
        return {"file": src, "dest": dest_path, "status": "error", "reason": source_stale}
    if dest.exists() and not overwrite:
        return {"file": src, "dest": dest_path, "status": "skipped", "reason": "destination already exists, set overwrite=True to replace"}

    executor = SafePathExecutor()
    authorized_source = executor.authorize_existing(source, access="write")
    if dest.exists():
        dest_stale = check_staleness(ctx, dest)
        if dest_stale:
            return {"file": src, "dest": dest_path, "status": "error", "reason": dest_stale}
        await save_file_version(ctx, dest, display_path=dest_path, tool_name=tool_name)
        authorized_dest = executor.authorize_existing(dest, access="write")
    else:
        authorized_dest = executor.authorize_target(dest, access="write")
    await save_file_version(ctx, source, display_path=src, tool_name=tool_name)
    move_result = executor.rename(authorized_source, authorized_dest, overwrite=overwrite)
    if not move_result.ok:
        return {"file": src, "dest": dest_path, "status": "error", "reason": move_result.error}
    move_file_tracking(ctx, source, dest)
    await ctx.authorization_service.move_created_path(
        ctx.workspace,
        source,
        dest,
        object_type="file",
        destination_created=destination_created,
    )
    return {"file": src, "dest": dest_path, "status": "moved"}


async def _move_directory(ctx: ToolContext, src: str, dest_path: str, overwrite: bool) -> dict:
    source, source_error = await _resolve_directory_path(ctx, src)
    if source_error:
        return {"file": src, "dest": dest_path, "status": "error", "reason": source_error}
    dest, dest_error = await _resolve_directory_path(ctx, dest_path, require_exists=False)
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
    destination_created = not dest.exists()
    if dest.exists() and not dest.is_dir():
        return {"file": src, "dest": dest_path, "status": "error", "reason": "Destination is a file, not a directory"}
    if dest.exists() and not overwrite:
        return {"file": src, "dest": dest_path, "status": "skipped", "reason": "destination already exists, set overwrite=True to replace"}

    source_root = source
    executor = SafePathExecutor()
    authorized_source = executor.authorize_existing(source, access="write")
    authorized_dest = executor.authorize_existing(dest, access="write") if dest.exists() else executor.authorize_target(dest, access="write")
    move_result = executor.rename(authorized_source, authorized_dest, overwrite=overwrite)
    if not move_result.ok and dest.exists() and overwrite and move_result.error_kind == "destination_exists":
        delete_result = executor.delete_tree(authorized_dest)
        if not delete_result.ok:
            clear_tree_tracking(ctx, source_root)
            clear_tree_tracking(ctx, dest)
            result = {"file": src, "dest": dest_path, "status": "error", "reason": delete_result.error}
            if delete_result.error_kind:
                result["error_kind"] = delete_result.error_kind
            return result
        authorized_dest = executor.authorize_target(dest, access="write")
        move_result = executor.rename(authorized_source, authorized_dest, overwrite=overwrite)
    if not move_result.ok:
        clear_tree_tracking(ctx, source_root)
        clear_tree_tracking(ctx, dest)
        result = {"file": src, "dest": dest_path, "status": "error", "reason": move_result.error}
        if move_result.error_kind:
            result["error_kind"] = move_result.error_kind
        return result

    clear_tree_tracking(ctx, source_root)
    clear_tree_tracking(ctx, dest)
    await ctx.authorization_service.move_created_path(
        ctx.workspace,
        source_root,
        dest,
        object_type="dir",
        destination_created=destination_created,
    )
    return {"file": src, "dest": dest_path, "status": "moved"}
