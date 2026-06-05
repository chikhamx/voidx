"""MCP client — stdio and SSE transports, JSON-RPC 2.0, crash-resilient.

Stdio pattern:
  asyncio.create_subprocess_exec(command, *args)
  stdin=PIPE, stdout=PIPE, stderr=PIPE

SSE pattern:
  HTTP POST to <url> for requests, SSE stream for responses.

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

import httpx

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

    async def _connect_streamable_http(self) -> None:
        """Connect to an MCP server via Streamable HTTP (MCP 2024-11-05).

        Unlike legacy SSE, there is no persistent GET stream. The client
        POSTs JSON-RPC requests directly to the server URL and receives
        responses as SSE events in the same HTTP response body.
        """
        url = self._config.url
        if not url:
            raise McpConnectionError(f"MCP server '{self._server_name}' has no URL configured")

        headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self._config.headers)
        if self._config.env:
            for key, value in self._config.env.items():
                if key.lower() == "authorization" and "Authorization" not in headers:
                    log.warning(
                        "MCP server '%s': reading Authorization from env is deprecated, "
                        "use the 'headers' field instead",
                        self._server_name,
                    )
                    headers["Authorization"] = value

        self._http_client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self.TOOL_CALL_TIMEOUT),
            trust_env=False,
        )
        self._streamable_url = url

    async def _connect_sse(self) -> None:
        """Connect to an MCP server via SSE (HTTP + Server-Sent Events).

        Per the MCP SSE spec:
          1. Client opens SSE connection to the server URL.
          2. Server sends an ``endpoint`` event with the POST URI.
          3. Client sends JSON-RPC messages via HTTP POST to that URI.
          4. Server responses arrive as SSE ``message`` events.
        """
        url = self._config.url
        if not url:
            raise McpConnectionError(f"MCP server '{self._server_name}' has no URL configured")

        headers: dict[str, str] = {
            "Accept": "text/event-stream",
        }
        headers.update(self._config.headers)
        if self._config.env:
            for key, value in self._config.env.items():
                if key.lower() == "authorization" and "Authorization" not in headers:
                    log.warning(
                        "MCP server '%s': reading Authorization from env is deprecated, "
                        "use the 'headers' field instead",
                        self._server_name,
                    )
                    headers["Authorization"] = value

        # Parse the SSE URL to derive the base for POST requests.
        # The user-provided URL is the SSE endpoint itself.
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        sse_path = parsed.path or "/sse"

        self._http_client = httpx.AsyncClient(
            base_url=base,
            headers=headers,
            timeout=httpx.Timeout(self.TOOL_CALL_TIMEOUT),
            trust_env=False,
        )

        # Start the SSE listener; it will set _sse_endpoint once the
        # server sends the "endpoint" event.
        self._sse_endpoint_event = asyncio.Event()
        self._sse_task = asyncio.create_task(self._read_sse_stream(sse_path))

        # Wait for the server to tell us the POST endpoint.
        try:
            await asyncio.wait_for(
                self._sse_endpoint_event.wait(),
                timeout=self.SSE_CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise McpConnectionError(
                f"MCP server '{self._server_name}' did not send endpoint event within {self.SSE_CONNECT_TIMEOUT}s"
            )

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

    async def _send_stdio(self, payload: dict[str, Any]) -> None:
        """Send a JSON-RPC message over stdio."""
        if self._writer is None:
            raise McpConnectionError("stdio writer not available")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

    async def _send_sse(self, payload: dict[str, Any]) -> None:
        """Send a JSON-RPC request via HTTP POST to the SSE endpoint."""
        if self._http_client is None:
            raise McpConnectionError("SSE HTTP client not available")
        endpoint = self._sse_endpoint or "/message"
        # The endpoint from the server may be a relative path or a full URL.
        # httpx handles both when using base_url + relative path.
        try:
            resp = await self._http_client.post(
                endpoint,
                json=payload,
                headers={"Accept": "application/json, text/event-stream"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise McpConnectionError(
                f"SSE POST failed for MCP server '{self._server_name}': {e}"
            ) from e

    async def _send_streamable_http(self, payload: dict[str, Any]) -> None:
        """Send a JSON-RPC request via Streamable HTTP.

        POST to the server URL. The response may be:
          - application/json: a single JSON-RPC response
          - text/event-stream: SSE events containing JSON-RPC responses
        Notifications (no id) are fire-and-forget.
        """
        if self._http_client is None:
            raise McpConnectionError("Streamable HTTP client not available")

        url = self._streamable_url
        has_id = "id" in payload
        req_id = payload.get("id")

        try:
            if has_id:
                # Request with id: stream the response to handle SSE
                async with self._http_client.stream(
                    "POST", url, json=payload,
                    headers={"Accept": "application/json, text/event-stream"},
                ) as resp:
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "")
                    if "text/event-stream" in content_type:
                        current_event = ""
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line:
                                current_event = ""
                                continue
                            if line.startswith("event:"):
                                current_event = line[len("event:"):].strip()
                                continue
                            if line.startswith("data:"):
                                data = line[len("data:"):].strip()
                                if not data:
                                    continue
                                if current_event == "message" or not current_event:
                                    try:
                                        msg = json.loads(data)
                                    except json.JSONDecodeError:
                                        log.warning(
                                            "Invalid SSE JSON from '%s': %s",
                                            self._server_name, data[:200],
                                        )
                                        continue
                                    self._dispatch_response(msg)
                                    # Stop reading once we get the response for our request
                                    if msg.get("id") == req_id:
                                        break
                    else:
                        # application/json: single response
                        body = await resp.aread()
                        try:
                            msg = json.loads(body)
                        except json.JSONDecodeError:
                            raise McpProtocolError(
                                f"Invalid JSON response from '{self._server_name}'"
                            )
                        self._dispatch_response(msg)
            else:
                # Notification: no response expected
                resp = await self._http_client.post(
                    url, json=payload,
                    headers={"Accept": "application/json, text/event-stream"},
                )
                resp.raise_for_status()
        except httpx.HTTPError as e:
            raise McpConnectionError(
                f"Streamable HTTP POST failed for MCP server '{self._server_name}': {e}"
            ) from e

    # ── internal: reading ───────────────────────────────────────────────

    async def _read_responses(self) -> None:
        """Read JSON-RPC responses from stdio stdout (background task)."""
        reader = self._reader
        if reader is None:
            return
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from MCP server '%s': %s", self._server_name, text[:200])
                    continue
                self._dispatch_response(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("stdio reader for '%s' exited: %s", self._server_name, e)
        finally:
            if self._healthy:
                self._healthy = False
                self._error_message = "stdio connection lost"
                for req in self._pending.values():
                    if not req.future.done():
                        req.future.set_exception(McpConnectionError("Connection lost"))
                self._pending.clear()

    async def _read_sse_stream(self, sse_path: str) -> None:
        """Read SSE events from the server (background task).

        Parses the SSE wire format:
          - ``event: endpoint`` + ``data: /path``  →  sets the POST endpoint
          - ``event: message``   + ``data: {json}``  →  dispatches JSON-RPC response
        """
        if self._http_client is None:
            return
        try:
            async with self._http_client.stream("GET", sse_path) as resp:
                resp.raise_for_status()
                current_event = ""
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        # Blank line = end of SSE event
                        current_event = ""
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:"):].strip()
                        continue
                    if line.startswith("data:"):
                        data = line[len("data:"):].strip()
                        if not data:
                            continue
                        # endpoint event: server tells us where to POST
                        if current_event == "endpoint":
                            self._sse_endpoint = data
                            log.info(
                                "MCP SSE '%s': endpoint = %s",
                                self._server_name, self._sse_endpoint,
                            )
                            evt = getattr(self, "_sse_endpoint_event", None)
                            if evt is not None:
                                evt.set()
                            continue
                        # message event: JSON-RPC response
                        if current_event == "message" or not current_event:
                            try:
                                msg = json.loads(data)
                            except json.JSONDecodeError:
                                log.warning("Invalid SSE JSON from '%s': %s", self._server_name, data[:200])
                                continue
                            self._dispatch_response(msg)
                            continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("SSE reader for '%s' exited: %s", self._server_name, e)
            if self._healthy:
                self._healthy = False
                self._error_message = f"SSE connection lost: {e}"
                for req in self._pending.values():
                    if not req.future.done():
                        req.future.set_exception(McpConnectionError("SSE connection lost"))
                self._pending.clear()

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

    # ── internal: cleanup ───────────────────────────────────────────────

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
