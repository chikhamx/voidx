"""Async stdio JSON-RPC client for Language Server Protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import Any

from voidx.lsp.errors import LspConnectionError, LspRequestError
from voidx.lsp.schema import LspServerConfig, parse_diagnostics

log = logging.getLogger(__name__)

NotificationHandler = Callable[[str, dict[str, Any]], None]


def encode_lsp_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


async def read_lsp_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, sep, value = line.decode("ascii", errors="replace").partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()

    raw_length = headers.get("content-length")
    if raw_length is None:
        raise LspConnectionError("LSP message missing Content-Length header")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise LspConnectionError(f"Invalid Content-Length: {raw_length}") from exc
    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))


class LspClient:
    def __init__(
        self,
        config: LspServerConfig,
        *,
        cwd: str,
        notification_handler: NotificationHandler | None = None,
    ) -> None:
        self.config = config
        self.cwd = cwd
        self._notification_handler = notification_handler
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._diagnostics: dict[str, list] = {}
        self._capabilities: dict[str, Any] = {}
        self._error_message = ""

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def connected(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def status(self) -> str:
        if self.connected:
            return "connected"
        if self._error_message:
            return "error"
        return "disconnected"

    @property
    def error_message(self) -> str:
        return self._error_message

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._capabilities

    async def start(self, *, root_uri: str, timeout: float = 10.0) -> None:
        if self.connected:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.config.command,
                *self.config.args,
                cwd=self.cwd,
                env=os.environ.copy(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._error_message = str(exc)
            raise LspConnectionError(f"Could not start {self.config.language} LSP: {exc}") from exc

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        result = await self.request("initialize", {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": _client_capabilities(),
            "workspaceFolders": [{"uri": root_uri, "name": "workspace"}],
        }, timeout=timeout)
        self._capabilities = result.get("capabilities", {}) if isinstance(result, dict) else {}
        await self.notify("initialized", {})

    async def stop(self) -> None:
        if self._process is None:
            return
        if self.connected:
            try:
                await self.request("shutdown", None, timeout=2.0)
            except Exception:
                pass
            try:
                await self.notify("exit", {})
            except Exception:
                pass
        try:
            await asyncio.wait_for(self._process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        self._process = None
        self._cancel_tasks()

    async def request(self, method: str, params: Any = None, *, timeout: float = 10.0) -> Any:
        if self._process is None or self._process.stdin is None:
            raise LspConnectionError("LSP client is not started")
        if self._process.returncode is not None:
            raise LspConnectionError(f"LSP server exited with code {self._process.returncode}")
        req_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise LspConnectionError(f"LSP request timed out: {method}") from exc
        if isinstance(response, dict) and response.get("error"):
            error = response["error"]
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise LspRequestError(f"{method}: {message}")
        return response.get("result") if isinstance(response, dict) else response

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def diagnostics_for(self, uri: str) -> list:
        return list(self._diagnostics.get(uri, []))

    def all_diagnostics(self) -> list:
        result: list = []
        for diagnostics in self._diagnostics.values():
            result.extend(diagnostics)
        return result

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise LspConnectionError("LSP client is not started")
        self._process.stdin.write(encode_lsp_message(payload))
        await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                message = await read_lsp_message(self._process.stdout)
                if message is None:
                    break
                self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error_message = str(exc)
            log.debug("LSP read loop failed for %s: %s", self.config.language, exc)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(LspConnectionError("LSP connection closed"))
            self._pending.clear()

    def _handle_message(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        if msg_id in self._pending:
            future = self._pending.pop(msg_id)
            if not future.done():
                future.set_result(message)
            return

        method = message.get("method")
        params = message.get("params")
        if method == "textDocument/publishDiagnostics" and isinstance(params, dict):
            uri = params.get("uri")
            if isinstance(uri, str):
                self._diagnostics[uri] = parse_diagnostics(uri, params.get("diagnostics", []))
        if isinstance(method, str) and isinstance(params, dict) and self._notification_handler:
            self._notification_handler(method, params)

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                if not self._error_message:
                    self._error_message = line.decode("utf-8", errors="replace").strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _cancel_tasks(self) -> None:
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._reader_task = None
        self._stderr_task = None


def _client_capabilities() -> dict[str, Any]:
    return {
        "textDocument": {
            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
            "definition": {"linkSupport": True},
            "references": {},
            "formatting": {},
            "publishDiagnostics": {"relatedInformation": True},
            "synchronization": {
                "didSave": True,
                "dynamicRegistration": False,
            },
        },
        "workspace": {
            "symbol": {"resolveSupport": {"properties": []}},
            "workspaceFolders": True,
        },
    }
