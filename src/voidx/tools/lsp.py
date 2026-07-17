"""LSP-backed code intelligence tools."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.diffing import make_file_diff
from voidx.lsp.errors import LspError, LspTimeoutError
from voidx.lsp.schema import LspDiagnostic, LspLocation, LspPosition, LspRange, LspSymbol
from voidx.lsp.service import LspService, _format_diagnostic, _format_location, _format_symbol
from voidx.permission.grants import AccessGrants, resolve_access
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    model_to_json_schema,
    _resolve_tool_path,
    _resolve_tool_path_for_access,
    _sandbox_paths_for_access,
    tool_timeout_metadata,
)
from voidx.tools.file.safe_path import SafePathExecutor
from voidx.tools.file.state import clear_read_coverage, record_mtime, save_file_version


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
        description="1-based line number. Required for definition and references operations.",
    )

    character: int = Field(
        default=0,
        ge=0,
        description="0-based character offset. Required for definition and references operations.",
    )

    include_declaration: bool = Field(
        default=True,
        description="For references only: include the symbol declaration in results.",
    )


class LspTool(BaseTool):
    id = "lsp"
    description = "Run language-server operations: diagnostics, definition lookup, references, and document symbols."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LspInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = LspInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        manager = getattr(ctx, "lsp_manager", None)
        if manager is None:
            return ToolResult(output="LSP manager not available.", metadata={"error": True})
        workspace = getattr(manager, "workspace", ctx.workspace)

        try:
            if inp.operation == "diagnostics":
                if inp.file_path is not None and not _is_read_allowed(ctx, inp.file_path, require_exists=True):
                    return _unauthorized_lsp_input()
                diagnostics = await manager.diagnostics(inp.file_path)
                allowed = [item for item in diagnostics if _is_read_allowed(ctx, item.path, require_exists=False)]
                if not allowed:
                    target = inp.file_path or "opened files"
                    return ToolResult(title="LSP diagnostics", output=f"No LSP diagnostics for {target}.", summary="diagnostics")
                output = "\n".join(_format_diagnostic(item, workspace) for item in allowed)
                return ToolResult(title="LSP diagnostics", output=output, summary="diagnostics")
            if inp.operation == "symbols":
                if inp.file_path is None:
                    output = await LspService(manager).symbols(None, "")
                    return ToolResult(title="LSP symbols", output=output, summary="symbols")
                if not _is_read_allowed(ctx, inp.file_path, require_exists=True):
                    return _unauthorized_lsp_input()
                symbols = await manager.document_symbols(inp.file_path)
                allowed = [item for item in symbols if not item.path or _is_read_allowed(ctx, item.path, require_exists=False)]
                output = "No LSP symbols found." if not allowed else "\n".join(_format_symbol(item, workspace) for item in allowed[:200])
                return ToolResult(title="LSP symbols", output=output, summary="symbols")
            if inp.operation == "definition":
                if inp.file_path is None:
                    return ToolResult(output="file_path is required for definition operation.", metadata={"error": True})
                if not _is_read_allowed(ctx, inp.file_path, require_exists=True):
                    return _unauthorized_lsp_input()
                locations = await manager.definition(inp.file_path, inp.line, inp.character)
                allowed = [item for item in locations if _is_read_allowed(ctx, item.path, require_exists=False)]
                output = "No definition found." if not allowed else "\n".join(_format_location(item, workspace) for item in allowed)
                return ToolResult(title="LSP definition", output=output, summary=f"definition at line {inp.line}")
            if inp.operation == "references":
                if inp.file_path is None:
                    return ToolResult(output="file_path is required for references operation.", metadata={"error": True})
                if not _is_read_allowed(ctx, inp.file_path, require_exists=True):
                    return _unauthorized_lsp_input()
                locations = await manager.references(
                    inp.file_path,
                    inp.line,
                    inp.character,
                    include_declaration=inp.include_declaration,
                )
                allowed = [item for item in locations if _is_read_allowed(ctx, item.path, require_exists=False)]
                output = "No references found." if not allowed else "\n".join(_format_location(item, workspace) for item in allowed)
                return ToolResult(title="LSP references", output=output, summary=f"references at line {inp.line}")
            return ToolResult(output=f"Unknown LSP operation: {inp.operation}", metadata={"error": True})
        except LspTimeoutError as exc:
            return ToolResult(
                output=f"LSP {inp.operation} timed out: {exc}",
                metadata=tool_timeout_metadata("lsp", operation=inp.operation),
            )
        except LspError as exc:
            return ToolResult(output=f"LSP {inp.operation} failed: {exc}", metadata={"error": True})


# ── Range formatting tool ────────────────────────────────────────────────────

class LspFormatInput(BaseModel):
    file_path: str = Field(description="File to format with the configured language server.")
    start_line: int = Field(ge=1, description="1-based inclusive start line.")
    start_character: int = Field(ge=0, description="0-based UTF-16 character offset.")
    end_line: int = Field(ge=1, description="1-based line containing the end position.")
    end_character: int = Field(ge=0, description="0-based UTF-16 end-exclusive character offset.")

    @model_validator(mode="after")
    def _validate_range(self) -> "LspFormatInput":
        if (self.end_line, self.end_character) < (self.start_line, self.start_character):
            raise ValueError("format range end must not precede start")
        return self

    def lsp_range(self) -> LspRange:
        return LspRange(
            start=LspPosition(line=self.start_line - 1, character=self.start_character),
            end=LspPosition(line=self.end_line - 1, character=self.end_character),
        )


class LspFormatTool(BaseTool):
    id = "lsp_format"
    description = "Format an explicit range in a file with its configured language server and write the result safely."

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
        path, error = await _resolve_tool_path_for_access(
            ctx,
            inp.file_path,
            write=True,
            require_exists=True,
            prompt_label="LSP format",
            allow_description="Allow formatting this file once",
            deny_description="Do not format this file",
        )
        if error is not None:
            return error
        assert path is not None
        old_text, read_error = _safe_read_text(path)
        if read_error is not None:
            return ToolResult(output=read_error, metadata={"error": True})
        range_error = _validate_lsp_range(old_text, inp.lsp_range())
        if range_error is not None:
            return ToolResult(output=range_error, metadata={"error": True})
        await save_file_version(ctx, path, display_path=inp.file_path, tool_name=self.id)
        try:
            changed, service_old_text, new_text = await service.format_range(inp.file_path, inp.lsp_range())
        except LspTimeoutError as exc:
            return ToolResult(
                output=f"LSP range format timed out: {exc}",
                metadata=tool_timeout_metadata("lsp_format", operation="range_format"),
            )
        except LspError as exc:
            return ToolResult(output=f"LSP range format failed: {exc}", metadata={"error": True})
        if service_old_text != old_text:
            return ToolResult(output="File changed while LSP range formatting was running.", metadata={"error": True})
        if not changed:
            record_mtime(ctx, path)
            return ToolResult(
                title="No formatting changes",
                output=f"No range formatting changes for {inp.file_path}.",
                metadata={"file": inp.file_path, "formatted": False},
            )
        write_error = _safe_write_text(path, new_text, expected_text=old_text)
        if write_error is not None:
            return ToolResult(output=write_error, metadata={"error": True})
        record_mtime(ctx, path)
        clear_read_coverage(ctx, path)
        diff = make_file_diff(inp.file_path, old_text, new_text)
        return ToolResult(
            title=f"Formatted {inp.file_path}",
            output=f"File range formatted: {inp.file_path}",
            metadata={"file": inp.file_path, "size": len(new_text), "formatted": True},
            diff=diff,
        )


def _safe_read_text(path: Path) -> tuple[str, str | None]:
    try:
        executor = SafePathExecutor()
        authorized = executor.authorize_existing(path, access="write")
        result = executor.read_text(authorized, encoding="utf-8", errors="replace")
    except OSError as exc:
        return "", str(exc)
    if not result.ok:
        return "", result.error
    return str(result.value), None


def _safe_write_text(path: Path, content: str, *, expected_text: str) -> str | None:
    try:
        executor = SafePathExecutor()
        authorized = executor.authorize_existing(path, access="write")
        result = executor.write_text(
            authorized,
            content,
            encoding="utf-8",
            expected_text=expected_text,
        )
    except OSError as exc:
        return str(exc)
    return None if result.ok else result.error


def _validate_lsp_range(text: str, range_: LspRange) -> str | None:
    lines = text.splitlines(keepends=True)
    if not lines:
        return "Cannot format an empty file."
    positions = (("start", range_.start), ("end", range_.end))
    for label, position in positions:
        if position.line == len(lines) and text.endswith(("\n", "\r")) and position.character == 0:
            continue
        if position.line >= len(lines):
            return f"Format range {label} line is outside the document."
        line_text = lines[position.line].rstrip("\r\n")
        if position.character > _utf16_length(line_text):
            return f"Format range {label} character is outside the line."
    return None


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _service(ctx: ToolContext) -> LspService | None:
    manager = _manager(ctx)
    if manager is None:
        return None
    return LspService(manager)


def _manager(ctx: ToolContext):
    return getattr(ctx, "lsp_manager", None)


def _access_grants(ctx: ToolContext) -> AccessGrants:
    if ctx.get_access_grants is not None:
        return ctx.get_access_grants()
    return AccessGrants.from_parts(
        readable_files=ctx.sandbox_readable_files,
        readable_dirs=ctx.sandbox_readable_dirs,
        writable_files=ctx.sandbox_writable_files,
        writable_dirs=ctx.sandbox_writable_dirs,
    )


def _is_read_allowed(ctx: ToolContext, file_path: str, *, require_exists: bool) -> bool:
    return resolve_access(
        ctx.workspace,
        file_path,
        access="read",
        access_grants=_access_grants(ctx),
        require_exists=require_exists,
    ).action == "allow"


def _unauthorized_lsp_input() -> ToolResult:
    return ToolResult(
        output="LSP input path is not authorized for reading.",
        metadata={"error": True, "error_kind": "unauthorized_path"},
    )
