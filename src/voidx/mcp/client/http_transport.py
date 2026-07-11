"""Streamable HTTP transport for the MCP client."""

from __future__ import annotations

import json

import httpx

from voidx.logging.tool_log import log_tool_event
from voidx.mcp.client.errors import McpConnectionError, McpProtocolError



class StreamableHttpTransportMixin:
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
                    log_tool_event("mcp_auth_deprecated", tool_name=self._server_name,
                                   message=f"MCP server '{self._server_name}': reading Authorization from env is deprecated, use the 'headers' field instead")
                    headers["Authorization"] = value

        self._http_client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self.TOOL_CALL_TIMEOUT),
            trust_env=False,
        )
        self._streamable_url = url


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
                                        log_tool_event("mcp_invalid_json", tool_name=self._server_name,
                                                       message=f"Invalid SSE JSON from '{self._server_name}': {data[:200]}")
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
