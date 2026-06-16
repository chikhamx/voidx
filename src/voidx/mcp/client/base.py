"""MCP client base: lifecycle, JSON-RPC, and request tracking."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from dataclasses import dataclass
from typing import Any

import httpx

from voidx.config import McpServerConfig
from voidx.mcp.client.errors import McpConnectionError, McpProtocolError, McpTimeoutError
from voidx.logging.tool_log import log_tool_event
from voidx.mcp.client.http_transport import StreamableHttpTransportMixin
from voidx.mcp.client.sse_transport import SseTransportMixin
from voidx.mcp.client.stdio_transport import StdioTransportMixin
from voidx.mcp.schema import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    McpCallResult,
    McpInitializeParams,
    McpToolDef,
    MCP_PROTOCOL_VERSION,
)

log = logging.getLogger(__name__)


@dataclass
class _PendingRequest:
    future: asyncio.Future
    method: str = ""


class McpClient(StreamableHttpTransportMixin, SseTransportMixin, StdioTransportMixin):
    """A single MCP server connection over stdio or SSE transport.

    Thread safety: not intended for concurrent access. The agent graph
    serializes tool calls within a single async context.
    """

    MAX_RECONNECT_ATTEMPTS = 3
    INIT_TIMEOUT = 15.0
    SSE_CONNECT_TIMEOUT = 10.0
    TOOL_CALL_TIMEOUT = 120.0
    LIST_TOOLS_TIMEOUT = 30.0

    def __init__(self, config: McpServerConfig) -> None:
        self._config = config
        self._transport = config.effective_transport
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._stderr_task: asyncio.Task | None = None
        self._read_task: asyncio.Task | None = None

        # SSE transport state
        self._http_client: httpx.AsyncClient | None = None
        self._sse_task: asyncio.Task | None = None
        self._sse_endpoint: str = ""
        self._sse_endpoint_event: asyncio.Event | None = None

        # Streamable HTTP transport state
        self._streamable_url: str = ""

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
        if not self._healthy:
            return False
        if self._transport in ("sse", "streamable-http"):
            return self._http_client is not None
        return self._proc is not None and self._proc.returncode is None

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
        """Connect to the MCP server, perform handshake, mark healthy."""
        if self._healthy:
            return
        try:
            if self._transport == "streamable-http":
                await self._connect_streamable_http()
            elif self._transport == "sse":
                await self._connect_sse()
            else:
                await self._spawn()
            await asyncio.wait_for(self._handshake(), timeout=self.INIT_TIMEOUT)
            self._initialized = True
            self._healthy = True
            self._reconnect_attempt = 0
            self._error_message = ""
            log.info("MCP client '%s' connected (%s)", self._server_name, self._transport)
        except Exception as e:
            await self._cleanup()
            self._error_message = str(e)
            raise McpConnectionError(f"Failed to initialize MCP server '{self._server_name}': {e}")

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._closed:
            return
        self._closed = True
        self._healthy = False
        if self._transport == "stdio" and self._writer and self._proc and self._proc.returncode is None:
            try:
                notif = JsonRpcNotification(method="shutdown")
                line = json.dumps(notif.to_dict(), ensure_ascii=False) + "\n"
                self._writer.write(line.encode("utf-8"))
                await asyncio.wait_for(self._writer.drain(), timeout=5.0)
            except Exception as exc:
                log_tool_event("mcp_shutdown_notification_failed", tool_name=self._server_name, message=str(exc))
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
        if arguments is not None:
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
        async with self._lock:
            if method != "initialize" and not self._healthy:
                if self._reconnect_attempt < self.MAX_RECONNECT_ATTEMPTS:
                    if not await self.reconnect():
                        raise McpConnectionError(self._error_message or "Not connected")
                else:
                    raise McpConnectionError(self._error_message or "Not connected")

            req_id = self._next_id()
            future: asyncio.Future[JsonRpcResponse] = asyncio.Future()

            self._pending[req_id] = _PendingRequest(future=future, method=method)

            request = JsonRpcRequest(id=req_id, method=method, params=params or {})
            payload = request.to_dict()

            try:
                await self._send_payload(payload)
            except Exception as e:
                self._pending.pop(req_id, None)
                raise McpConnectionError(f"Failed to send request to '{self._server_name}': {e}") from e

            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                self._pending.pop(req_id, None)
                raise McpTimeoutError(
                    f"Request '{method}' to MCP server '{self._server_name}' timed out after {timeout}s"
                )

    async def _send_notification(self, notif: JsonRpcNotification) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        payload = notif.to_dict()
        try:
            await self._send_payload(payload)
        except Exception as e:
            log.warning("Failed to send notification to '%s': %s", self._server_name, e)

    async def _send_payload(self, payload: dict[str, Any]) -> None:
        """Send a JSON-RPC payload via the active transport."""
        if self._transport == "streamable-http":
            await self._send_streamable_http(payload)
        elif self._transport == "sse":
            await self._send_sse(payload)
        else:
            await self._send_stdio(payload)


    # ── internal: response dispatch / cleanup ───────────────────────────

    def _dispatch_response(self, msg: dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message to the correct pending future."""
        # JSON-RPC responses have an "id" field
        msg_id = msg.get("id")
        if msg_id is not None:
            pending = self._pending.pop(msg_id, None)
            if pending is not None and not pending.future.done():
                if "error" in msg:
                    error = msg["error"]
                    pending.future.set_exception(McpProtocolError(
                        f"MCP error from '{self._server_name}': "
                        f"{error.get('message', error)}"
                    ))
                else:
                    pending.future.set_result(JsonRpcResponse(
                        id=msg_id,
                        result=msg.get("result"),
                        error=msg.get("error"),
                    ))
            return

        # JSON-RPC notifications / requests from server (no id or method-based)
        method = msg.get("method", "")
        if method:
            log.debug("MCP notification from '%s': %s", self._server_name, method)

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _cleanup(self) -> None:
        """Clean up subprocess/tasks and HTTP client."""
        # Cancel background tasks
        for task_name in ("_read_task", "_stderr_task", "_sse_task"):
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

        # Close SSE HTTP client
        http_client = self._http_client
        self._http_client = None
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception:
                pass

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
