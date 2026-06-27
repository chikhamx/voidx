"""LSP-backed code intelligence tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from voidx.diffing import make_file_diff
from voidx.lsp.errors import LspError
from voidx.lsp.service import LspService
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
from voidx.tools.file_state import clear_read_coverage, record_mtime, save_file_version


class LspInput(BaseModel):
    operation: Literal[
        "diagnostics",
        "definition",
        "references",
        "symbols",
    ] = Field(description="The LSP operation to perform.")

    file_path: str | None = Field(
        default=None,
        description="Absolute or relative path to the file. "
        "Required for all operations except diagnostics (when omitted, returns cached diagnostics for opened files).",
    )

    line: int = Field(
        default=1,
        ge=1,
        description="1-based line number. Must be set for definition and references.",
    )

    character: int = Field(
        default=0,
        ge=0,
        description="0-based character offset. Must be set for definition and references.",
    )

    include_declaration: bool = Field(
        default=True,
        description="Include the symbol declaration in results. Only for references operation.",
    )


class LspTool(BaseTool):
    id = "lsp"
    description = "Language server operations: diagnostics, definitions, references, and document symbols."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LspInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = LspInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        service = _service(ctx)
        if service is None:
            return ToolResult(output="LSP manager not available.", metadata={"error": True})

        try:
            if inp.operation == "diagnostics":
                output = await service.diagnostics(inp.file_path)
                return ToolResult(title="LSP diagnostics", output=output, summary="diagnostics")
            elif inp.operation == "symbols":
                query = ""
                output = await service.symbols(inp.file_path, query)
                return ToolResult(title="LSP symbols", output=output, summary="symbols")
            elif inp.operation == "definition":
                if inp.file_path is None:
                    return ToolResult(output="file_path is required for definition operation.", metadata={"error": True})
                output = await service.definition(inp.file_path, inp.line, inp.character)
                return ToolResult(title="LSP definition", output=output, summary=f"definition at line {inp.line}")
            elif inp.operation == "references":
                if inp.file_path is None:
                    return ToolResult(output="file_path is required for references operation.", metadata={"error": True})
                output = await service.references(
                    inp.file_path, inp.line, inp.character,
                    include_declaration=inp.include_declaration,
                )
                return ToolResult(title="LSP references", output=output, summary=f"references at line {inp.line}")
            else:
                return ToolResult(output=f"Unknown LSP operation: {inp.operation}", metadata={"error": True})
        except LspError as exc:
            return ToolResult(output=f"LSP {inp.operation} failed: {exc}", metadata={"error": True})


# ── LspFormatTool kept but not registered ───────────────────────────────────

class LspFormatInput(BaseModel):
    file_path: str = Field(description="File to format with the configured language server.")


class LspFormatTool(BaseTool):
    id = "lsp_format"
    description = "Format a file with its configured language server. Writes the formatted content back to disk."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LspFormatInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = LspFormatInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        service = _service(ctx)
        if service is None:
            return ToolResult(output="LSP manager not available.", metadata={"error": True})
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is not None and path.exists() and path.is_file():
            await save_file_version(ctx, path, display_path=inp.file_path, tool_name=self.id)
        try:
            changed, old_text, new_text = await service.format(inp.file_path)
        except LspError as exc:
            return ToolResult(output=f"LSP format failed: {exc}", metadata={"error": True})
        if not changed:
            if path is not None and path.exists():
                record_mtime(ctx, path)
            return ToolResult(output=f"No formatting changes for {inp.file_path}.")
        if path is not None and path.exists():
            record_mtime(ctx, path)
            clear_read_coverage(ctx, path)

        diff = make_file_diff(inp.file_path, old_text, new_text)
        return ToolResult(
            title=f"Formatted {inp.file_path}",
            output=f"File formatted: {inp.file_path}",
            metadata={"file": inp.file_path, "size": len(new_text)},
            diff=diff,
        )


def _service(ctx: ToolContext) -> LspService | None:
    manager = getattr(ctx, "lsp_manager", None)
    if manager is None:
        return None
    return LspService(manager)
