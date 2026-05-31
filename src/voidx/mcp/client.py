"""MCP client — stdio transport, JSON-RPC 2.0, crash-resilient.

Spawning pattern:
  asyncio.create_subprocess_exec(command, *args)
  stdin=PIPE, stdout=PIPE, stderr=PIPE

Wire format:
  Line-delimited JSON (one JSON object per line, \\n terminated).
  Requests and responses interleaved via JSON-RPC 2.0 id matching.

Lifecycle:
  created → start() → initialized → list_tools() → call_tool()* → stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from dataclasses import dataclass, field
from typing import Any

from voidx.config import McpServerConfig
from voidx.mcp.schema import (
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcNotification,
    McpCallResult,
    McpInitializeParams,
    McpToolDef,
    MCP_PROTOCOL_VERSION,
)

log = logging.getLogger(__name__)

# ── error types ───────────────────────────────────────────────────────────


class McpConnectionError(Exception):
    """Connection-level error (process died, transport failure)."""


class McpProtocolError(Exception):
    """Protocol-level error (invalid JSON, unexpected response)."""


class McpTimeoutError(Exception):
    """Operation timed out."""


# ── pending request tracker ──────────────────────────────────────────────


@dataclass
class _PendingRequest:
    future: asyncio.Future
    method: str = ""


# ── client ────────────────────────────────────────────────────────────────


class McpClient:
    """A single MCP server connection over stdio transport.

    Thread safety: not intended for concurrent access. The agent graph
    serializes tool calls within a single async context.
    """

    MAX_RECONNECT_ATTEMPTS = 3
    INIT_TIMEOUT = 45.0
    TOOL_CALL_TIMEOUT = 120.0
    LIST_TOOLS_TIMEOUT = 30.0

    def __init__(self, config: McpServerConfig) -> None:
        self._config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._stderr_task: asyncio.Task | None = None
        self._read_task: asyncio.Task | None = None

        self._request_id = 0
        self._pending: dict[int, _PendingRequest] = {}
        self._initialized = False
        self._healthy = False
        self._error_message = ""
        self._reconnect_attempt = 0
        self._closed = False
        self._server_name = config.name

        # Synchronisation: only one call at a time per client
        self._lock = asyncio.Lock()

    # ── properties ──────────────────────────────────────────────────────

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def healthy(self) -> bool:
        return self._healthy and self._proc is not None and self._proc.returncode is None

    @property
    def status(self) -> str:
        if self._healthy:
            return "connected"
        if self._error_message:
            return "error"
        return "disconnected"

    @property
    def error_message(self) -> str:
        return self._error_message

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the subprocess, perform handshake, mark healthy."""
        if self._healthy:
            return
        try:
            await self._spawn()
            await asyncio.wait_for(self._handshake(), timeout=self.INIT_TIMEOUT)
            self._initialized = True
            self._healthy = True
            self._reconnect_attempt = 0
            self._error_message = ""
            log.info("MCP client '%s' connected", self._server_name)
        except Exception as e:
            await self._cleanup()
            self._error_message = str(e)
            raise McpConnectionError(f"Failed to initialize MCP server '{self._server_name}': {e}")

    async def stop(self) -> None:
        """Graceful shutdown. Sends shutdown notification then kills."""
        if self._closed:
            return
        self._closed = True
        self._healthy = False
        if self._writer and self._proc and self._proc.returncode is None:
            try:
                notif = JsonRpcNotification(method="shutdown")
                line = json.dumps(notif.to_dict(), ensure_ascii=False) + "\n"
                self._writer.write(line.encode("utf-8"))
                await asyncio.wait_for(self._writer.drain(), timeout=5.0)
            except Exception:
                pass
        await self._cleanup()
        log.info("MCP client '%s' stopped", self._server_name)

    async def reconnect(self) -> bool:
        """Attempt to reconnect a failed server."""
        self._healthy = False
        self._reconnect_attempt += 1
        if self._reconnect_attempt > self.MAX_RECONNECT_ATTEMPTS:
            self._error_message = f"Reconnect failed after {self.MAX_RECONNECT_ATTEMPTS} attempts"
            return False
        await self._cleanup()
        try:
            await self.start()
            return True
        except McpConnectionError:
            return False

    # ── protocol operations ──────────────────────────────────────────────

    async def list_tools(self) -> list[McpToolDef]:
        """Discover tools from the MCP server."""
        resp = await self._request("tools/list", timeout=self.LIST_TOOLS_TIMEOUT)
        result = resp.result
        if not isinstance(result, dict):
            raise McpProtocolError(f"Expected dict from tools/list, got {type(result).__name__}")
        tools_data = result.get("tools", [])
        if not isinstance(tools_data, list):
            raise McpProtocolError(f"Expected list of tools, got {type(tools_data).__name__}")
        return [
            McpToolDef(
                name=t.get("name", ""),
                description=t.get("description", ""),
                inputSchema=t.get("inputSchema", {}),
            )
            for t in tools_data
            if isinstance(t, dict) and t.get("name")
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None) -> McpCallResult:
        """Call a tool on the MCP server."""
        timeout = timeout or self.TOOL_CALL_TIMEOUT
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments
        resp = await self._request("tools/call", params, timeout=timeout)
        result = resp.result
        if not isinstance(result, dict):
            raise McpProtocolError(f"Expected dict from tools/call, got {type(result).__name__}")
        content = result.get("content", [])
        if not isinstance(content, list):
            content = []
        is_error = bool(result.get("isError", False))
        return McpCallResult(
            content=content,
            isError=is_error,
            structured_content=result.get("structuredContent"),
        )

    # ── internal: transport ─────────────────────────────────────────────

    async def _spawn(self) -> None:
        """Spawn the subprocess with configured command/args/env."""
        cmd = self._config.command
        if not cmd:
            raise McpConnectionError(f"MCP server '{self._server_name}' has no command configured")
        args = [cmd] + list(self._config.args)

        env = None
        if self._config.env:
            import os
            env = {**os.environ, **self._config.env}

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as e:
            raise McpConnectionError(
                f"Command not found for MCP server '{self._server_name}': {cmd}"
            ) from e
        except PermissionError as e:
            raise McpConnectionError(
                f"Permission denied for MCP server '{self._server_name}': {cmd}"
            ) from e

        if self._proc.stdin is None or self._proc.stdout is None or self._proc.stderr is None:
            await self._cleanup()
            raise McpConnectionError(f"Failed to open pipes for MCP server '{self._server_name}'")

        self._writer = self._proc.stdin
        self._reader = self._proc.stdout

        # Read stderr in background (logging only)
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # Start background reader for incoming JSON-RPC responses
        self._read_task = asyncio.create_task(self._read_responses())

    async def _cleanup(self) -> None:
        """Clean up subprocess and tasks."""
        # Cancel background tasks
        for task_name in ("_read_task", "_stderr_task"):
            task = getattr(self, task_name, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            setattr(self, task_name, None)

        self._reader = None
        self._writer = None

        # Reject all pending requests
        for req in self._pending.values():
            if not req.future.done():
                req.future.set_exception(McpConnectionError("Connection closed"))
        self._pending.clear()

        # Terminate subprocess
        proc = self._proc
        self._proc = None
        if proc and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass

    async def _read_stderr(self) -> None:
        """Read stderr and forward to debug output."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.debug("[MCP stderr:%s] %s", self._server_name, text)
        except Exception:
            pass

    # ── internal: protocol ──────────────────────────────────────────────

    async def _handshake(self) -> None:
        """Perform MCP initialization handshake."""
        params = McpInitializeParams()
        resp = await self._request("initialize", params.to_dict(), timeout=self.INIT_TIMEOUT)
        result = resp.result
        if not isinstance(result, dict):
            raise McpProtocolError(
                f"Expected dict from initialize, got {type(result).__name__}"
            )
        server_version = result.get("protocolVersion", "unknown")
        if server_version != MCP_PROTOCOL_VERSION:
            log.warning(
                "MCP server '%s' protocol v%s, expected v%s",
                self._server_name, server_version, MCP_PROTOCOL_VERSION,
            )

        # Send initialized notification (no response expected)
        notif = JsonRpcNotification(method="notifications/initialized")
        await self._send_notification(notif)

    async def _request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> JsonRpcResponse:
        """Send a JSON-RPC request and wait for the matching response."""
        if method != "initialize" and not self._healthy and self._reconnect_attempt < self.MAX_RECONNECT_ATTEMPTS:
            if await self.reconnect():
                pass
            else:
                raise McpConnectionError(self._error_message or "Not connected")
        elif method != "initialize" and not self._healthy:
            raise McpConnectionError(self._error_message or "Not connected")

        async with self._lock:
            req_id = self._next_id()
            future: asyncio.Future[JsonRpcResponse] = asyncio.Future()
            self._pending[req_id] = _PendingRequest(future=future, method=method)

            request = JsonRpcRequest(
                id=req_id,
                method=method,
                params=params or {},
            )
            try:
                await self._send_request(request)
                response = await asyncio.wait_for(future, timeout=timeout)
                return response
            except asyncio.TimeoutError:
                self._pending.pop(req_id, None)
                raise McpTimeoutError(
                    f"MCP server '{self._server_name}' did not respond to '{method}' within {timeout}s"
                )
            except (McpConnectionError, McpProtocolError):
                self._pending.pop(req_id, None)
                raise
            except Exception as e:
                self._pending.pop(req_id, None)
                raise McpConnectionError(f"Unexpected error in MCP request '{method}': {e}")

    async def _send_request(self, request: JsonRpcRequest) -> None:
        """Send a JSON-RPC request as a single line."""
        if self._writer is None:
            raise McpConnectionError("No transport writer available")
        line = json.dumps(request.to_dict(), ensure_ascii=False) + "\n"
        try:
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self._healthy = False
            raise McpConnectionError(f"Transport write error: {e}") from e

    async def _send_notification(self, notification: JsonRpcNotification) -> None:
        """Send a JSON-RPC notification (fire-and-forget)."""
        if self._writer is None:
            return
        line = json.dumps(notification.to_dict(), ensure_ascii=False) + "\n"
        try:
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._healthy = False

    async def _read_responses(self) -> None:
        """Background reader: parse inbound JSON-RPC messages and resolve pending futures."""
        if self._reader is None:
            return
        buffer = ""
        try:
            while True:
                chunk = await self._reader.read(65536)
                if not chunk:
                    # EOF — process exited
                    self._healthy = False
                    self._error_message = "Process exited unexpectedly"
                    # Fail all pending
                    for req in self._pending.values():
                        if not req.future.done():
                            req.future.set_exception(
                                McpConnectionError("Process exited mid-request")
                            )
                    self._pending.clear()
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                lines = buffer.split("\n")
                buffer = lines[-1]  # partial last line
                for line in lines[:-1]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._dispatch_line(line)
                    except Exception:
                        log.exception("MCP response dispatch error")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("MCP reader error")

    def _dispatch_line(self, line: str) -> None:
        """Parse a JSON line and route to pending request or drop (notification)."""
        data = json.loads(line)
        if not isinstance(data, dict):
            return

        msg_id = data.get("id")
        if msg_id is not None:
            # It's a response — resolve pending request
            pending = self._pending.pop(int(msg_id), None)
            if pending is None:
                log.warning("Unexpected response id=%s for server '%s'", msg_id, self._server_name)
                return
            if not pending.future.done():
                if "error" in data and data["error"] is not None:
                    err = data["error"]
                    err_msg = err.get("message", "Unknown error")
                    err_code = err.get("code", -1)
                    pending.future.set_exception(
                        McpProtocolError(f"MCP error [{err_code}]: {err_msg}")
                    )
                else:
                    pending.future.set_result(JsonRpcResponse(
                        id=int(msg_id),
                        result=data.get("result"),
                    ))

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def __aenter__(self) -> McpClient:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()
