"""Operations exposed by the LSP application layer."""

from __future__ import annotations

from typing import Protocol

from voidx.lsp.domain import LspDiagnostic, LspLocation, LspRange, LspSymbol


class LspOperations(Protocol):
    @property
    def workspace(self) -> str: ...

    async def diagnostics(self, file_path: str | None = None) -> list[LspDiagnostic]: ...

    async def document_symbols(self, file_path: str) -> list[LspSymbol]: ...

    async def workspace_symbols(self, query: str) -> list[LspSymbol]: ...

    async def definition(self, file_path: str, line: int, character: int) -> list[LspLocation]: ...

    async def references(
        self,
        file_path: str,
        line: int,
        character: int,
        *,
        include_declaration: bool = True,
    ) -> list[LspLocation]: ...

    async def format_range(self, file_path: str, range_: LspRange) -> tuple[bool, str, str]: ...


__all__ = ["LspOperations"]
