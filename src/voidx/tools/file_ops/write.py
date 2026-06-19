from __future__ import annotations

from voidx.diffing import make_file_diff
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
from voidx.tools.file_state import (
    check_staleness,
    clear_read_coverage,
    record_mtime,
    record_read_range,
    save_file_version,
)
from pydantic import BaseModel, Field

from .read import _split_display_lines
from .types import DisplayLines


class FileWriteInput(BaseModel):
    file_path: str = Field(description="Path to write the file to")
    content: str = Field(
        description=(
            "Content to write. Keep under ~150 lines for best results; for larger files write "
            "a small non-empty skeleton with prefix/suffix markers, read it, then use edit to fill it incrementally."
        )
    )


class FileWriteTool(BaseTool):
    id = "write"
    description = (
        "Write content to a file. Creates parent directories. Overwrites existing files. "
        "For files around 150 lines or larger, write a skeleton first (imports, class/function "
        "signatures, docstrings, and prefix/suffix markers), read it, then use edit "
        "to replace or insert implementation blocks incrementally. This avoids output "
        "truncation and reduces wait time."
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
            await save_file_version(ctx, path, display_path=inp.file_path, tool_name=self.id)
            old_content = path.read_text(encoding="utf-8", errors="replace")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp.content, encoding="utf-8")
        size = len(inp.content)
        record_mtime(ctx, path)
        line_count = len(_split_display_lines(inp.content).lines)
        if line_count > 0:
            record_read_range(ctx, path, 1, line_count)
        else:
            clear_read_coverage(ctx, path)

        diff = make_file_diff(
            inp.file_path,
            old_content,
            inp.content,
            old_label=f"a/{inp.file_path}" if old_content else "/dev/null",
            new_label=f"b/{inp.file_path}",
        )

        output = f"File written: {inp.file_path} ({size} bytes)"
        line_count = len(_split_display_lines(inp.content).lines)
        if line_count > 200:
            output += (
                f"\nNote: This file is large ({line_count} lines). "
                "For future writes of similar size, consider writing a skeleton first "
                "with prefix/suffix markers, reading it, and using edit to add content incrementally."
            )

        return ToolResult(
            title=f"Wrote {size} bytes",
            output=output,
            summary=f"Wrote {size} bytes",
            metadata={"file": inp.file_path, "size": size},
            diff=diff,
        )
