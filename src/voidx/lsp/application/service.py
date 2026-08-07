"""High-level LSP operations for tools and slash commands."""

from __future__ import annotations

from pathlib import Path

from voidx.lsp.application.manager import LspManager
from voidx.lsp.domain import LspDiagnostic, LspLocation, LspRange, LspSymbol


class LspOperationsService:
    def __init__(self, manager: LspManager) -> None:
        self._manager = manager

    @property
    def workspace(self) -> str:
        return self._manager.workspace

    async def diagnostics(self, file_path: str | None = None) -> list[LspDiagnostic]:
        return await self._manager.diagnostics(file_path)

    async def document_symbols(self, file_path: str) -> list[LspSymbol]:
        return await self._manager.document_symbols(file_path)

    async def workspace_symbols(self, query: str) -> list[LspSymbol]:
        return await self._manager.workspace_symbols(query)

    async def definition(self, file_path: str, line: int, character: int) -> list[LspLocation]:
        return await self._manager.definition(file_path, line, character)

    async def references(
        self,
        file_path: str,
        line: int,
        character: int,
        *,
        include_declaration: bool = True,
    ) -> list[LspLocation]:
        return await self._manager.references(
            file_path,
            line,
            character,
            include_declaration=include_declaration,
        )

    async def format_range(self, file_path: str, range_: LspRange) -> tuple[bool, str, str]:
        return await self._manager.formatted_range_text(file_path, range_)


class LspService:
    def __init__(self, manager: LspManager) -> None:
        self._manager = manager

    async def diagnostics(self, file_path: str | None = None) -> str:
        diagnostics = await self._manager.diagnostics(file_path)
        if not diagnostics:
            target = file_path or "opened files"
            return f"No LSP diagnostics for {target}."
        return "\n".join(_format_diagnostic(item, self._manager.workspace) for item in diagnostics)

    async def symbols(self, file_path: str | None = None, query: str = "") -> str:
        if file_path:
            symbols = await self._manager.document_symbols(file_path)
        elif query:
            symbols = await self._manager.workspace_symbols(query)
        else:
            return "Provide file_path for document symbols or query for workspace symbols."
        if not symbols:
            return "No LSP symbols found."
        return "\n".join(_format_symbol(item, self._manager.workspace) for item in symbols[:200])

    async def definition(self, file_path: str, line: int, character: int) -> str:
        locations = await self._manager.definition(file_path, line, character)
        if not locations:
            return "No definition found."
        return "\n".join(_format_location(item, self._manager.workspace) for item in locations)

    async def references(
        self,
        file_path: str,
        line: int,
        character: int,
        *,
        include_declaration: bool = True,
    ) -> str:
        locations = await self._manager.references(
            file_path,
            line,
            character,
            include_declaration=include_declaration,
        )
        if not locations:
            return "No references found."
        return "\n".join(_format_location(item, self._manager.workspace) for item in locations[:200])

    async def format(self, file_path: str) -> tuple[bool, str, str]:
        return await self._manager.format_document(file_path)


    async def format_range(
        self,
        file_path: str,
        range_: LspRange,
    ) -> tuple[bool, str, str]:
        return await self._manager.formatted_range_text(file_path, range_)


def _format_diagnostic(diagnostic: LspDiagnostic, workspace: str) -> str:
    level = _severity_label(diagnostic.severity)
    loc = _format_range_location(diagnostic.path, diagnostic.range, workspace)
    source = f" [{diagnostic.source}]" if diagnostic.source else ""
    code = f" {diagnostic.code}" if diagnostic.code else ""
    return f"{loc}: {level}{source}{code}: {diagnostic.message}"


def _format_symbol(symbol: LspSymbol, workspace: str) -> str:
    path = _rel(symbol.path, workspace) if symbol.path else ""
    if symbol.selection_range is not None:
        path = _format_range_location(symbol.path, symbol.selection_range, workspace)
    name = symbol.name
    if symbol.container_name:
        name = f"{symbol.container_name}.{name}"
    kind = f"kind={symbol.kind}" if symbol.kind is not None else "symbol"
    return f"{path}: {name} ({kind})" if path else f"{name} ({kind})"


def _format_location(location: LspLocation, workspace: str) -> str:
    if location.range is None:
        return _rel(location.path, workspace)
    return _format_range_location(location.path, location.range, workspace)


def _format_range_location(path: str, range_data, workspace: str) -> str:
    rel = _rel(path, workspace)
    line = range_data.start.line + 1
    character = range_data.start.character
    return f"{rel}:{line}:{character}"


def _rel(path: str, workspace: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(workspace).resolve())).replace("\\", "/")
    except ValueError:
        return path


def _severity_label(severity: int | None) -> str:
    return {
        1: "error",
        2: "warning",
        3: "info",
        4: "hint",
    }.get(severity, "diagnostic")
