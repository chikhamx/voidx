"""File operation tools — read, write, edit. Deterministic, typed I/O."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.diffing import make_file_diff
from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult, resolve_safe
from voidx.tools.file_state import check_staleness, record_mtime


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

        record_mtime(ctx, path)

        return ToolResult(
            title=f"Read {len(sliced)} lines from {inp.file_path}",
            output="\n".join(numbered),
            metadata={"file": inp.file_path, "lines": len(sliced), "total_lines": len(lines)},
        )


class FileWriteInput(BaseModel):
    file_path: str = Field(description="Path to write the file to")
    content: str = Field(
        description=(
            "Content to write. Keep under ~150 lines for best results; for larger files write "
            "a skeleton with unique placeholders and use edit to replace them incrementally."
        )
    )


class FileWriteTool(BaseTool):
    id = "write"
    description = (
        "Write content to a file. Creates parent directories. Overwrites existing files. "
        "For files around 150 lines or larger, write a skeleton first (imports, class/function "
        "signatures, docstrings, and unique placeholders), then use edit to replace placeholders "
        "with implementation blocks incrementally. This avoids output truncation and reduces wait time."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(FileWriteInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = FileWriteInput.model_validate(args)
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is None:
            return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
        if path.exists():
            stale = check_staleness(ctx, path)
            if stale:
                return ToolResult(output=stale, metadata={"error": True})

        old_content = ""
        if path.exists():
            old_content = path.read_text(encoding="utf-8", errors="replace")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp.content, encoding="utf-8")
        size = len(inp.content)
        record_mtime(ctx, path)

        diff = make_file_diff(
            inp.file_path,
            old_content,
            inp.content,
            old_label=f"a/{inp.file_path}" if old_content else "/dev/null",
            new_label=f"b/{inp.file_path}",
        )

        output = f"File written: {inp.file_path} ({size} bytes)"
        line_count = inp.content.count("\n") + 1
        if line_count > 200:
            output += (
                f"\nNote: This file is large ({line_count} lines). "
                "For future writes of similar size, consider writing a skeleton first "
                "and using edit to add content incrementally."
            )

        return ToolResult(
            title=f"Wrote {size} bytes to {inp.file_path}",
            output=output,
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

        stale = check_staleness(ctx, path)
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
        record_mtime(ctx, path)

        diff = make_file_diff(inp.file_path, original, content)

        return ToolResult(
            title=f"Edited {inp.file_path} ({len(inp.edits)} edits)",
            output=f"File edited: {inp.file_path} ({len(inp.edits)} replacements)\n{diff}",
            metadata={"file": inp.file_path, "replacements": len(inp.edits)},
            diff=diff,
        )
