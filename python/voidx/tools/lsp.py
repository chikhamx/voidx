"""LSP-backed code intelligence tools."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.lsp.errors import LspError
from voidx.lsp.service import LspService
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe


class LspDiagnosticsInput(BaseModel):
    file_path: str | None = Field(
        default=None,
        description="File to check. If omitted, returns cached diagnostics for already opened files.",
    )


class LspSymbolsInput(BaseModel):
    file_path: str | None = Field(default=None, description="File for document symbols.")
    query: str = Field(default="", description="Workspace symbol query when file_path is omitted.")


class LspPositionInput(BaseModel):
    file_path: str = Field(description="File containing the position.")
    line: int = Field(description="1-based line number.", ge=1)
    character: int = Field(default=0, description="0-based character offset.", ge=0)


class LspReferencesInput(LspPositionInput):
    include_declaration: bool = Field(default=True, description="Include the symbol declaration in results.")


class LspFormatInput(BaseModel):
    file_path: str = Field(description="File to format with the configured language server.")


class LspDiagnosticsTool(BaseTool):
    id = "lsp_diagnostics"
    description = "Get LSP diagnostics for a file, or cached diagnostics for opened files when file_path is omitted."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LspDiagnosticsInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = LspDiagnosticsInput.model_validate(args)
        service = _service(ctx)
        if service is None:
            return ToolResult(output="LSP manager not available.", metadata={"error": True})
        try:
            output = await service.diagnostics(inp.file_path)
            return ToolResult(title="LSP diagnostics", output=output)
        except LspError as exc:
            return ToolResult(output=f"LSP diagnostics failed: {exc}", metadata={"error": True})


class LspSymbolsTool(BaseTool):
    id = "lsp_symbols"
    description = "List document symbols for file_path, or workspace symbols for query."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LspSymbolsInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = LspSymbolsInput.model_validate(args)
        service = _service(ctx)
        if service is None:
            return ToolResult(output="LSP manager not available.", metadata={"error": True})
        try:
            output = await service.symbols(inp.file_path, inp.query)
            return ToolResult(title="LSP symbols", output=output)
        except LspError as exc:
            return ToolResult(output=f"LSP symbols failed: {exc}", metadata={"error": True})


class LspDefinitionTool(BaseTool):
    id = "lsp_definition"
    description = "Find definition locations for a symbol at file_path:line:character."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LspPositionInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = LspPositionInput.model_validate(args)
        service = _service(ctx)
        if service is None:
            return ToolResult(output="LSP manager not available.", metadata={"error": True})
        try:
            output = await service.definition(inp.file_path, inp.line, inp.character)
            return ToolResult(title="LSP definition", output=output)
        except LspError as exc:
            return ToolResult(output=f"LSP definition failed: {exc}", metadata={"error": True})


class LspReferencesTool(BaseTool):
    id = "lsp_references"
    description = "Find references for a symbol at file_path:line:character."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LspReferencesInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = LspReferencesInput.model_validate(args)
        service = _service(ctx)
        if service is None:
            return ToolResult(output="LSP manager not available.", metadata={"error": True})
        try:
            output = await service.references(
                inp.file_path,
                inp.line,
                inp.character,
                include_declaration=inp.include_declaration,
            )
            return ToolResult(title="LSP references", output=output)
        except LspError as exc:
            return ToolResult(output=f"LSP references failed: {exc}", metadata={"error": True})


class LspFormatTool(BaseTool):
    id = "lsp_format"
    description = "Format a file with its configured language server. Writes the formatted content back to disk."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LspFormatInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = LspFormatInput.model_validate(args)
        service = _service(ctx)
        if service is None:
            return ToolResult(output="LSP manager not available.", metadata={"error": True})
        try:
            changed, old_text, new_text = await service.format(inp.file_path)
        except LspError as exc:
            return ToolResult(output=f"LSP format failed: {exc}", metadata={"error": True})
        path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
        if path is not None and path.exists():
            ctx.file_mtimes[str(path.resolve())] = path.stat().st_mtime
        if not changed:
            return ToolResult(output=f"No formatting changes for {inp.file_path}.")

        from voidx.ui.output.diff import make_file_diff
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
