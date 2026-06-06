"""File operation tools — read, write, edit. Deterministic, typed I/O."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from pydantic import BaseModel, Field

from voidx.diffing import make_file_diff
from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult, resolve_safe


def _check_staleness(ctx: ToolContext, resolved: Path) -> str | None:
    """Return error message if file was modified since last read, else None."""
    key = str(resolved.resolve())
    if key not in ctx.file_mtimes:
        return None
    if not resolved.exists():
        return f"File deleted since last read: {resolved}"
    current_mtime = resolved.stat().st_mtime
    if current_mtime != ctx.file_mtimes[key]:
        return (
            f"File was modified since last read: {resolved}. "
            "Please re-read the file before editing."
        )
    return None


def _record_mtime(ctx: ToolContext, resolved: Path) -> None:
    """Record file mtime after successful read or write."""
    if resolved.exists():
        ctx.file_mtimes[str(resolved.resolve())] = resolved.stat().st_mtime


class FileReadInput(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file")
    offset: int | None = Field(default=None, description="Line number to start reading from (1-based)")
    limit: int | None = Field(default=None, description="Maximum number of lines to read")


class FileReadTool(BaseTool):
    id = "read"
    description = "Read a file. Returns content with line numbers. Use offset/limit for large files."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileReadInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileReadInput.model_validate(args)
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is None:
            return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
        if not path.exists():
            return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})
        if path.is_dir():
            return ToolResult(output=f"Path is a directory: {inp.file_path}", metadata={"error": True})

        text = path.read_text(encoding="utf-8", errors="replace")
        if text.endswith("\n"):
            text = text[:-1]
        lines = text.split("\n")
        start = (inp.offset or 1) - 1
        if start >= len(lines):
            return ToolResult(
                title=f"Read 0 lines from {inp.file_path}",
                output=f"Offset {inp.offset} is beyond end of file (file has {len(lines)} lines).",
                metadata={"file": inp.file_path, "lines": 0, "total_lines": len(lines)},
            )
        end = start + (inp.limit or len(lines))
        sliced = lines[start:end]

        numbered = []
        for i, line in enumerate(sliced, start=start + 1):
            numbered.append(f"{i}\t{line}")

        _record_mtime(ctx, path)

        return ToolResult(
            title=f"Read {len(sliced)} lines from {inp.file_path}",
            output="\n".join(numbered),
            metadata={"file": inp.file_path, "lines": len(sliced), "total_lines": len(lines)},
        )


class FileWriteInput(BaseModel):
    file_path: str = Field(description="Path to write the file to")
    content: str = Field(description="Content to write")


class FileWriteTool(BaseTool):
    id = "write"
    description = "Write content to a file. Creates parent directories. Overwrites existing files."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileWriteInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileWriteInput.model_validate(args)
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is None:
            return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
        if path.exists():
            stale = _check_staleness(ctx, path)
            if stale:
                return ToolResult(output=stale, metadata={"error": True})

        old_content = ""
        if path.exists():
            old_content = path.read_text(encoding="utf-8", errors="replace")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp.content, encoding="utf-8")
        size = len(inp.content)
        _record_mtime(ctx, path)

        diff = make_file_diff(
            inp.file_path,
            old_content,
            inp.content,
            old_label=f"a/{inp.file_path}" if old_content else "/dev/null",
            new_label=f"b/{inp.file_path}",
        )

        return ToolResult(
            title=f"Wrote {size} bytes to {inp.file_path}",
            output=f"File written: {inp.file_path} ({size} bytes)",
            metadata={"file": inp.file_path, "size": size},
            diff=diff,
        )


class EditEntry(BaseModel):
    old_string: str = Field(description="Exact string to replace. Must exist at least once in the file.")
    new_string: str = Field(description="String to replace with")


class FileEditInput(BaseModel):
    file_path: str = Field(description="Path to edit")
    edits: list[EditEntry] = Field(description="List of edits to apply atomically. Use a single entry for one change.")


class FileEditTool(BaseTool):
    id = "edit"
    description = (
        "Replace strings in a single file atomically. Each old_string must match "
        "exactly once — provide more context if it appears multiple times. "
        "Edits apply in order — later edits see earlier results."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileEditInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileEditInput.model_validate(args)
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is None:
            return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
        if not path.exists():
            return ToolResult(output=f"File not found: {inp.file_path}", metadata={"error": True})

        if not inp.edits:
            return ToolResult(
                output="No edits provided. The 'edits' array must contain at least one entry.",
                metadata={"error": True},
            )

        stale = _check_staleness(ctx, path)
        if stale:
            return ToolResult(output=stale, metadata={"error": True})

        original = path.read_text(encoding="utf-8", errors="replace")
        content = original

        for i, edit in enumerate(inp.edits):
            if not edit.old_string:
                return ToolResult(output=f"Edit {i}: old_string must not be empty")
            if edit.old_string == edit.new_string:
                return ToolResult(output=f"Edit {i}: old_string and new_string must differ")

            count = content.count(edit.old_string)
            if count == 0:
                return ToolResult(
                    output=f"Edit {i}: old_string not found in {inp.file_path}",
                    metadata={"error": True},
                )
            if count > 1:
                return ToolResult(
                    output=(
                        f"Edit {i}: old_string matches {count} times in {inp.file_path}. "
                        "Provide more context to make the match unique, or use write to replace the entire file."
                    ),
                    metadata={"error": True, "match_count": count},
                )

            content = content.replace(edit.old_string, edit.new_string)

        path.write_text(content, encoding="utf-8")
        _record_mtime(ctx, path)

        diff = make_file_diff(inp.file_path, original, content)

        return ToolResult(
            title=f"Edited {inp.file_path} ({len(inp.edits)} edits)",
            output=f"File edited: {inp.file_path} ({len(inp.edits)} replacements)\n{diff}",
            metadata={"file": inp.file_path, "replacements": len(inp.edits)},
            diff=diff,
        )


class ApplyPatchInput(BaseModel):
    patch: str = Field(description="Unified diff to apply across one or more files")
    dry_run: bool = Field(default=False, description="Validate and report changes without writing files")


class ApplyPatchTool(BaseTool):
    id = "apply_patch"
    description = (
        "Apply a unified diff across one or more text files. Validates all hunks before writing, "
        "supports dry_run, and rejects renames, binary patches, and paths outside the sandbox."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(ApplyPatchInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = ApplyPatchInput.model_validate(args)
        try:
            patches = _parse_unified_diff(inp.patch)
        except ValueError as exc:
            return ToolResult(output=str(exc), metadata={"error": True})

        if not patches:
            return ToolResult(output="Patch contains no file changes.", metadata={"error": True})

        plans: list[_PatchPlan] = []
        combined_diffs: list[str] = []
        files_meta: list[dict] = []

        for file_index, file_patch in enumerate(patches):
            if not file_patch.hunks:
                return ToolResult(
                    output=f"File patch {file_index}: no hunks found.",
                    metadata={"error": True, "file": file_patch.display_path},
                )
            if file_patch.is_rename:
                return ToolResult(
                    output=f"Rename patches are not supported: {file_patch.old_path} -> {file_patch.new_path}",
                    metadata={"error": True, "file": file_patch.display_path},
                )

            path = resolve_safe(ctx.workspace, file_patch.display_path, ctx.sandbox_extra_paths)
            if path is None:
                return ToolResult(
                    output=f"Path traversal blocked: {file_patch.display_path}",
                    metadata={"error": True, "file": file_patch.display_path},
                )

            if file_patch.status == "create":
                if path.exists():
                    return ToolResult(
                        output=f"Create patch target already exists: {file_patch.display_path}",
                        metadata={"error": True, "file": file_patch.display_path},
                    )
                original = ""
                existed = False
            else:
                if not path.exists():
                    return ToolResult(
                        output=f"Patch target not found: {file_patch.display_path}",
                        metadata={"error": True, "file": file_patch.display_path},
                    )
                if path.is_dir():
                    return ToolResult(
                        output=f"Patch target is a directory: {file_patch.display_path}",
                        metadata={"error": True, "file": file_patch.display_path},
                    )
                stale = _check_staleness(ctx, path)
                if stale:
                    return ToolResult(output=stale, metadata={"error": True, "file": file_patch.display_path})
                original = path.read_text(encoding="utf-8", errors="replace")
                existed = True

            try:
                new_content = _apply_file_patch(original, file_patch)
            except _PatchApplyError as exc:
                return ToolResult(
                    output=str(exc),
                    metadata={
                        "error": True,
                        "file": file_patch.display_path,
                        "hunk": exc.hunk_index,
                    },
                )

            if file_patch.status == "delete" and new_content.strip():
                return ToolResult(
                    output=f"Delete patch did not remove all content: {file_patch.display_path}",
                    metadata={"error": True, "file": file_patch.display_path},
                )

            old_label = "/dev/null" if file_patch.status == "create" else f"a/{file_patch.display_path}"
            new_label = "/dev/null" if file_patch.status == "delete" else f"b/{file_patch.display_path}"
            diff = make_file_diff(
                file_patch.display_path,
                original,
                "" if file_patch.status == "delete" else new_content,
                old_label=old_label,
                new_label=new_label,
            )
            if diff:
                combined_diffs.append(diff)
            added, removed = _patch_stats(file_patch)
            files_meta.append({
                "file": file_patch.display_path,
                "status": file_patch.status,
                "added": added,
                "removed": removed,
            })
            plans.append(_PatchPlan(
                path=path,
                display_path=file_patch.display_path,
                status=file_patch.status,
                existed=existed,
                original=original,
                new_content=new_content,
            ))

        if not inp.dry_run:
            written: list[_PatchPlan] = []
            try:
                for plan in plans:
                    if plan.status == "delete":
                        plan.path.unlink()
                    else:
                        plan.path.parent.mkdir(parents=True, exist_ok=True)
                        plan.path.write_text(plan.new_content, encoding="utf-8")
                    written.append(plan)
            except Exception as exc:
                _restore_written_plans(written)
                return ToolResult(
                    output=f"Patch write failed and rollback was attempted: {exc}",
                    metadata={"error": True},
                )
            for plan in plans:
                if plan.status == "delete":
                    ctx.file_mtimes.pop(str(plan.path.resolve()), None)
                else:
                    _record_mtime(ctx, plan.path)

        action = "validated" if inp.dry_run else "applied"
        output_lines = [
            f"{item['status']}: {item['file']} (+{item['added']} -{item['removed']})"
            for item in files_meta
        ]
        return ToolResult(
            title=f"Patch {action} for {len(plans)} files",
            output="\n".join(output_lines),
            metadata={
                "dry_run": inp.dry_run,
                "changed_files": len(plans),
                "files": files_meta,
            },
            diff="\n".join(combined_diffs),
        )


@dataclass
class _HunkLine:
    kind: str
    text: str


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[_HunkLine]


@dataclass
class _FilePatch:
    old_path: str
    new_path: str
    hunks: list[_Hunk]

    @property
    def status(self) -> str:
        if self.old_path == "/dev/null":
            return "create"
        if self.new_path == "/dev/null":
            return "delete"
        return "modify"

    @property
    def display_path(self) -> str:
        return self.new_path if self.new_path != "/dev/null" else self.old_path

    @property
    def is_rename(self) -> bool:
        return (
            self.old_path != "/dev/null"
            and self.new_path != "/dev/null"
            and self.old_path != self.new_path
        )


@dataclass
class _PatchPlan:
    path: Path
    display_path: str
    status: str
    existed: bool
    original: str
    new_content: str


class _PatchApplyError(Exception):
    def __init__(self, message: str, hunk_index: int | None = None) -> None:
        super().__init__(message)
        self.hunk_index = hunk_index


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _parse_unified_diff(patch: str) -> list[_FilePatch]:
    lines = patch.splitlines()
    patches: list[_FilePatch] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("GIT binary patch") or line.startswith("Binary files "):
            raise ValueError("Binary patches are not supported.")
        if line.startswith("rename from ") or line.startswith("rename to "):
            raise ValueError("Rename patches are not supported.")
        if not line.startswith("--- "):
            i += 1
            continue

        old_path = _parse_diff_path(line[4:])
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise ValueError(f"Malformed patch for {old_path}: missing +++ header.")
        new_path = _parse_diff_path(lines[i][4:])
        i += 1

        file_patch = _FilePatch(old_path=old_path, new_path=new_path, hunks=[])
        while i < len(lines):
            line = lines[i]
            if line.startswith("--- "):
                break
            if line.startswith("GIT binary patch") or line.startswith("Binary files "):
                raise ValueError("Binary patches are not supported.")
            if line.startswith("rename from ") or line.startswith("rename to "):
                raise ValueError("Rename patches are not supported.")
            if not line.startswith("@@ "):
                i += 1
                continue

            match = _HUNK_RE.match(line)
            if match is None:
                raise ValueError(f"Malformed hunk header in {file_patch.display_path}: {line}")
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            i += 1

            hunk_lines: list[_HunkLine] = []
            while i < len(lines):
                current = lines[i]
                if current.startswith("@@ ") or current.startswith("--- "):
                    break
                if current.startswith("\\ No newline at end of file"):
                    i += 1
                    continue
                if not current:
                    raise ValueError(f"Malformed hunk line in {file_patch.display_path}: empty unprefixed line")
                kind = current[0]
                if kind not in {" ", "+", "-"}:
                    break
                hunk_lines.append(_HunkLine(kind=kind, text=current[1:]))
                i += 1

            file_patch.hunks.append(_Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=hunk_lines,
            ))
        patches.append(file_patch)
    return patches


def _parse_diff_path(raw: str) -> str:
    path = raw.strip().split("\t", 1)[0]
    if path.startswith('"'):
        raise ValueError("Quoted diff paths are not supported.")
    if " " in path:
        path = path.split(" ", 1)[0]
    if path == "/dev/null":
        return path
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    if not path:
        raise ValueError("Patch contains an empty file path.")
    return path


def _apply_file_patch(original: str, file_patch: _FilePatch) -> str:
    lines, trailing_newline = _split_content(original)
    line_delta = 0
    for index, hunk in enumerate(file_patch.hunks):
        old_lines = [line.text for line in hunk.lines if line.kind in {" ", "-"}]
        new_lines = [line.text for line in hunk.lines if line.kind in {" ", "+"}]
        expected = max(0, hunk.old_start - 1 + line_delta)
        match_index = _find_hunk_match(lines, old_lines, expected)
        if match_index is None:
            raise _PatchApplyError(
                f"Hunk {index} failed to apply in {file_patch.display_path}.",
                hunk_index=index,
            )
        lines = lines[:match_index] + new_lines + lines[match_index + len(old_lines):]
        line_delta += len(new_lines) - len(old_lines)
        if file_patch.status == "create":
            trailing_newline = True
    return _join_content(lines, trailing_newline)


def _split_content(content: str) -> tuple[list[str], bool]:
    if not content:
        return [], False
    return content.splitlines(), content.endswith("\n")


def _join_content(lines: list[str], trailing_newline: bool) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def _find_hunk_match(lines: list[str], old_lines: list[str], expected: int) -> int | None:
    if not old_lines:
        return min(max(expected, 0), len(lines))
    bounded = min(max(expected, 0), max(len(lines) - len(old_lines), 0))
    if _lines_match(lines, old_lines, bounded):
        return bounded

    candidates = _nearby_candidates(lines, old_lines, bounded, normalized=False)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return None

    candidates = _nearby_candidates(lines, old_lines, bounded, normalized=True)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _nearby_candidates(
    lines: list[str],
    old_lines: list[str],
    expected: int,
    *,
    normalized: bool,
) -> list[int]:
    candidates: list[int] = []
    seen: set[int] = set()
    max_start = max(len(lines) - len(old_lines), 0)
    for delta in range(1, 4):
        for pos in (expected - delta, expected + delta):
            if pos < 0 or pos > max_start or pos in seen:
                continue
            seen.add(pos)
            if _lines_match(lines, old_lines, pos, normalized=normalized):
                candidates.append(pos)
    return candidates


def _lines_match(
    lines: list[str],
    old_lines: list[str],
    pos: int,
    *,
    normalized: bool = False,
) -> bool:
    if pos < 0 or pos + len(old_lines) > len(lines):
        return False
    current = lines[pos:pos + len(old_lines)]
    if normalized:
        return [line.strip() for line in current] == [line.strip() for line in old_lines]
    return current == old_lines


def _patch_stats(file_patch: _FilePatch) -> tuple[int, int]:
    added = 0
    removed = 0
    for hunk in file_patch.hunks:
        added += sum(1 for line in hunk.lines if line.kind == "+")
        removed += sum(1 for line in hunk.lines if line.kind == "-")
    return added, removed


def _restore_written_plans(plans: list[_PatchPlan]) -> None:
    for plan in reversed(plans):
        try:
            if plan.existed:
                plan.path.write_text(plan.original, encoding="utf-8")
            elif plan.path.exists():
                plan.path.unlink()
        except Exception:
            continue
