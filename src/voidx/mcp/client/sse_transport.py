"""Legacy SSE transport for the MCP client."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlparse, urlunparse

import httpx

from voidx.mcp.client.errors import McpConnectionError

log = logging.getLogger(__name__)


class SseTransportMixin:
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
