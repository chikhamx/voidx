"""LSP server lifecycle and document operations."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from voidx.lsp.client import LspClient
from voidx.lsp.config import install_hint_for, language_for_path, load_lsp_servers
from voidx.lsp.errors import LspConnectionError, LspServerUnavailable
from voidx.lsp.schema import (
    LspDiagnostic,
    LspDoctorCheck,
    LspLocation,
    LspRuntimeStatus,
    LspServerConfig,
    LspSymbol,
    file_uri,
    parse_document_symbols,
    parse_locations,
)
from voidx.tools.base import resolve_safe


class LspManager:
    def __init__(self, workspace: str) -> None:
        self.workspace = str(Path(workspace).resolve())
        self._servers: dict[str, LspServerConfig] = {}
        self._clients: dict[str, LspClient] = {}
        self._errors: dict[str, str] = {}
        self._open_docs: dict[str, tuple[str, int, str]] = {}
        self._initialized = False
        self._initializing = False
        self._initialization_error = ""
        self._initialize_lock = asyncio.Lock()

    @property
    def servers(self) -> dict[str, LspServerConfig]:
        return self._servers

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def initializing(self) -> bool:
        return self._initializing

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            self._initializing = True
            self._initialization_error = ""
            try:
                servers = await asyncio.to_thread(load_lsp_servers, self.workspace)
            except Exception as exc:
                self._initialization_error = str(exc)
                raise LspServerUnavailable(f"LSP server configuration failed: {exc}") from exc
            else:
                self._servers = servers
                self._initialized = True
            finally:
                self._initializing = False

    def has_available_server(self) -> bool:
        if not self._initialized:
            return False
        return any(
            config.enabled and bool(config.resolved_command)
            for config in self._servers.values()
        )

    async def stop_all(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        self._open_docs.clear()
        await asyncio.gather(*(client.stop() for client in clients), return_exceptions=True)

    async def restart(self, language: str | None = None) -> None:
        await self.initialize()
        if language:
            client = self._clients.pop(language, None)
            if client is not None:
                await client.stop()
            self._errors.pop(language, None)
            for uri, (doc_language, _, _) in list(self._open_docs.items()):
                if doc_language == language:
                    self._open_docs.pop(uri, None)
            return
        await self.stop_all()
        self._errors.clear()
        self._servers = await asyncio.to_thread(load_lsp_servers, self.workspace)
        self._initialized = True

    def statuses(self) -> list[LspRuntimeStatus]:
        if not self._initialized:
            status = "error" if self._initialization_error else "initializing"
            return [LspRuntimeStatus(
                language="*",
                command="",
                status=status,
                error_message=self._initialization_error or "Loading LSP server configuration.",
            )]
        result: list[LspRuntimeStatus] = []
        for language, config in self._servers.items():
            client = self._clients.get(language)
            open_docs = sum(1 for doc_language, _, _ in self._open_docs.values() if doc_language == language)
            if not config.enabled:
                status = "disabled"
            elif language in self._errors:
                status = "error"
            elif client is not None and client.connected:
                status = "connected"
            else:
                status = "disconnected"
            result.append(LspRuntimeStatus(
                language=language,
                command=" ".join([config.command, *config.args]).strip(),
                status=status,
                pid=client.pid if client is not None else None,
                open_documents=open_docs,
                error_message=self._errors.get(language, "") or (client.error_message if client else ""),
            ))
        return result

    def doctor(self) -> list[LspDoctorCheck]:
        if not self._initialized:
            return [LspDoctorCheck(
                language="*",
                command="",
                enabled=True,
                available=False,
                error_message=self._initialization_error or "LSP servers are still initializing.",
            )]
        checks: list[LspDoctorCheck] = []
        for language, config in self._servers.items():
            resolved = config.resolved_command or _resolve_command(config.command)
            available = bool(resolved)
            error = ""
            if not config.enabled:
                error = "Server disabled in config."
            elif not available:
                error = f"Command not found: {config.command}"
            checks.append(LspDoctorCheck(
                language=language,
                command=" ".join([config.command, *config.args]).strip(),
                enabled=config.enabled,
                available=available,
                resolved_path=resolved,
                install_hint=install_hint_for(language) if not available else "",
                error_message=error,
                detected_source=config.detected_source,
            ))
        return checks

    async def diagnostics(self, file_path: str | None = None, *, wait: float = 0.35) -> list[LspDiagnostic]:
        if file_path:
            client, uri = await self.open_document(file_path)
            if wait > 0:
                await asyncio.sleep(wait)
            return client.diagnostics_for(uri)
        result: list[LspDiagnostic] = []
        for client in self._clients.values():
            result.extend(client.all_diagnostics())
        return result

    async def document_symbols(self, file_path: str) -> list[LspSymbol]:
        client, uri = await self.open_document(file_path)
        value = await client.request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })
        return parse_document_symbols(uri, value)

    async def workspace_symbols(self, query: str) -> list[LspSymbol]:
        await self.initialize()
        symbols: list[LspSymbol] = []
        for language in self._servers:
            try:
                client = await self._ensure_client(language)
            except LspServerUnavailable:
                continue
            value = await client.request("workspace/symbol", {"query": query})
            symbols.extend(parse_document_symbols(file_uri(self.workspace), value))
        return symbols

    async def definition(self, file_path: str, line: int, character: int) -> list[LspLocation]:
        client, uri = await self.open_document(file_path)
        value = await client.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": _position(line, character),
        })
        return parse_locations(value)

    async def references(
        self,
        file_path: str,
        line: int,
        character: int,
        *,
        include_declaration: bool = True,
    ) -> list[LspLocation]:
        client, uri = await self.open_document(file_path)
        value = await client.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": _position(line, character),
            "context": {"includeDeclaration": include_declaration},
        })
        return parse_locations(value)

    async def format_document(self, file_path: str) -> tuple[bool, str, str]:
        path = self._resolve_path(file_path)
        old_text = path.read_text(encoding="utf-8", errors="replace")
        client, uri = await self.open_document(file_path)
        edits = await client.request("textDocument/formatting", {
            "textDocument": {"uri": uri},
            "options": {"tabSize": 4, "insertSpaces": True},
        })
        new_text = apply_text_edits(old_text, edits if isinstance(edits, list) else [])
        if new_text == old_text:
            return False, old_text, old_text
        path.write_text(new_text, encoding="utf-8")
        await self.open_document(file_path)
        return True, old_text, new_text

    async def open_document(self, file_path: str) -> tuple[LspClient, str]:
        path = self._resolve_path(file_path)
        await self.initialize()
        language = self._language_for(path)
        client = await self._ensure_client(language)
        uri = file_uri(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        _, version, previous = self._open_docs.get(uri, (language, 0, ""))
        if version == 0:
            version = 1
            await client.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri,
                    "languageId": language,
                    "version": version,
                    "text": text,
                },
            })
        elif previous != text:
            version += 1
            await client.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            })
        self._open_docs[uri] = (language, version, text)
        return client, uri

    async def _ensure_client(self, language: str) -> LspClient:
        await self.initialize()
        config = self._servers.get(language)
        if config is None or not config.enabled:
            raise LspServerUnavailable(f"No enabled LSP server for language: {language}")
        client = self._clients.get(language)
        if client is not None and client.connected:
            return client
        self._check_command(config)
        client = LspClient(config, cwd=self.workspace)
        try:
            await client.start(root_uri=file_uri(self.workspace))
        except LspConnectionError as exc:
            self._errors[language] = str(exc)
            raise
        self._errors.pop(language, None)
        self._clients[language] = client
        return client

    def _resolve_path(self, file_path: str) -> Path:
        path = resolve_safe(self.workspace, file_path)
        if path is None:
            raise LspServerUnavailable(f"Path traversal blocked: {file_path}")
        if not path.exists():
            raise LspServerUnavailable(f"File not found: {file_path}")
        if path.is_dir():
            raise LspServerUnavailable(f"Path is a directory: {file_path}")
        return path

    def _language_for(self, path: Path) -> str:
        language = language_for_path(path, self._servers)
        if language is None:
            raise LspServerUnavailable(f"No LSP server configured for file type: {path.suffix or path.name}")
        return language

    def _check_command(self, config: LspServerConfig) -> None:
        resolved = config.resolved_command or _resolve_command(config.command)
        if resolved:
            return
        message = f"Command not found for {config.language} LSP: {config.command}"
        self._errors[config.language] = message
        raise LspServerUnavailable(message)


def _position(line: int, character: int) -> dict[str, int]:
    return {"line": max(line - 1, 0), "character": max(character, 0)}


def _resolve_command(command: str) -> str:
    path = Path(command)
    if path.is_absolute() and path.exists():
        return str(path)
    resolved = shutil.which(command)
    return resolved or ""


def apply_text_edits(text: str, edits: list[Any]) -> str:
    parsed: list[tuple[int, int, str]] = []
    for edit in edits:
        if not isinstance(edit, dict) or "range" not in edit:
            continue
        range_data = edit["range"]
        if not isinstance(range_data, dict):
            continue
        start = range_data.get("start", {})
        end = range_data.get("end", {})
        if not isinstance(start, dict) or not isinstance(end, dict):
            continue
        start_offset = _offset_for_position(text, int(start.get("line", 0)), int(start.get("character", 0)))
        end_offset = _offset_for_position(text, int(end.get("line", 0)), int(end.get("character", 0)))
        parsed.append((start_offset, end_offset, str(edit.get("newText", ""))))
    result = text
    for start, end, new_text in sorted(parsed, key=lambda item: item[0], reverse=True):
        result = result[:start] + new_text + result[end:]
    return result


def _offset_for_position(text: str, line: int, character: int) -> int:
    lines = text.splitlines(keepends=True)
    if line <= 0:
        return min(character, len(lines[0]) if lines else len(text))
    if line >= len(lines):
        return len(text)
    return sum(len(part) for part in lines[:line]) + min(character, len(lines[line]))
