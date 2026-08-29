"""WebSocket server wrapper for the v2 JSON-RPC gateway."""

from __future__ import annotations

import asyncio
import contextlib
import json
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import Server, ServerConnection, serve

from voidx.observability import log_internal_error
from voidx.observability.tool_log import log_tool_event

from voidx.presentation.gateway.session import GatewaySession
from voidx.presentation.protocol.v2.incremental import ClientCapabilities
from voidx.presentation.protocol.v2.envelope import (
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
    ParseError,
    parse_jsonrpc_message,
)


SEND_QUEUE_MAXSIZE = 256
WEBSOCKET_SEND_TIMEOUT_SECONDS = 30.0



class _WebSocketClient:
    def __init__(
        self,
        websocket: ServerConnection,
        *,
        queue_maxsize: int = SEND_QUEUE_MAXSIZE,
        send_timeout: float = WEBSOCKET_SEND_TIMEOUT_SECONDS,
    ) -> None:
        self._websocket = websocket
        self._send_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_maxsize)
        self._send_task: asyncio.Task[None] | None = None
        self._closed = False
        self._dropped_messages = 0
        self._send_timeout = send_timeout

    async def start(self) -> None:
        if self._send_task is None:
            self._send_task = asyncio.create_task(self._send_loop(), name="voidx-gateway-ws-send-loop")

    async def send_text(self, text: str, *, priority: bool = False) -> None:
        if self._closed:
            return
        try:
            self._send_queue.put_nowait(text)
            return
        except asyncio.QueueFull:
            if priority:
                self._drop_one_queued_message()
                self._send_queue.put_nowait(text)
                return
            self._dropped_messages += 1
            queue_size = self._send_queue.qsize()
            message = (
                "Gateway send queue full; dropping message "
                f"count={self._dropped_messages} queue_size={queue_size}"
            )
            log_tool_event("gateway_send_queue_full", tool_name="gateway", message=message)

    # Methods whose messages are state snapshots or refresh triggers;
    # older instances are safe to discard in favor of newer ones.
    _DROPPABLE_METHODS = frozenset({"workspace.snapshot", "refresh.requested"})

    def _drop_one_queued_message(self) -> None:
        """Drop the oldest droppable message; fall back to the queue head."""
        drained: list[str] = []
        dropped = False
        while True:
            try:
                text = self._send_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not dropped and self._is_droppable(text):
                self._send_queue.task_done()
                dropped = True
                continue
            drained.append(text)
        for text in drained:
            self._send_queue.put_nowait(text)
        if not dropped:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._send_queue.get_nowait()
                self._send_queue.task_done()

    @staticmethod
    def _is_droppable(text: str) -> bool:
        try:
            envelope = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return False
        method = envelope.get("method") if isinstance(envelope, dict) else None
        return method in _WebSocketClient._DROPPABLE_METHODS

    async def _send_loop(self) -> None:
        try:
            while True:
                text = await self._send_queue.get()
                try:
                    if text is None:
                        return
                    await asyncio.wait_for(self._websocket.send(text), timeout=self._send_timeout)
                except asyncio.TimeoutError:
                    queue_size = self._send_queue.qsize()
                    message = (
                        "Gateway websocket send timed out "
                        f"after {self._send_timeout:.1f}s queue_size={queue_size}"
                    )
                    log_tool_event("gateway_websocket_send_timeout", tool_name="gateway", message=message)
                    self._closed = True
                    return
                except Exception as exc:
                    queue_size = self._send_queue.qsize()
                    message = f"Gateway websocket send failed queue_size={queue_size}: {exc}"
                    log_internal_error(exc, context="gateway_websocket_send")
                    log_tool_event("gateway_websocket_send_failed", tool_name="gateway", message=message)
                    self._closed = True
                    return
                finally:
                    self._send_queue.task_done()
        finally:
            self._closed = True

    async def close(self) -> None:
        if self._closed and self._send_task is None:
            return
        self._closed = True
        if self._send_task is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._send_queue.put_nowait(None)
            try:
                await asyncio.wait_for(self._send_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._send_task
        with contextlib.suppress(Exception):
            await self._websocket.close()


class GatewayServer:
    def __init__(
        self,
        session: GatewaySession,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str = "",
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._token = token
        self._server: Server | None = None
        self._bound_port: int | None = None

    @property
    def url(self) -> str:
        port = self._bound_port if self._bound_port is not None else self._port
        suffix = f"?token={self._token}" if self._token else ""
        return f"ws://{self._host}:{port}/{suffix}"

    async def start(self) -> None:
        if self._server is not None:
            return
        server: Server | None = None
        try:
            await self._session.initialize_provisional_lifecycle()
            server = await serve(self._handle, self._host, self._port)
            socket = server.sockets[0]
            self._bound_port = int(socket.getsockname()[1])
            self._server = server
        except BaseException:
            if server is not None:
                server.close()
                with contextlib.suppress(Exception):
                    await server.wait_closed()
            self._server = None
            self._bound_port = None
            with contextlib.suppress(Exception):
                await self._session.close_provisional_lifecycle()
            raise

    async def stop(self) -> None:
        server = self._server
        if server is None:
            return
        try:
            server.close()
            await server.wait_closed()
        finally:
            self._server = None
            self._bound_port = None
            await self._session.close_provisional_lifecycle()

    @staticmethod
    def _capabilities_from_websocket(websocket: ServerConnection) -> ClientCapabilities:
        request = getattr(websocket, "request", None)
        path = getattr(request, "path", "")
        query = parse_qs(urlparse(path).query)
        values: list[str] = []
        for raw in query.get("cap", []):
            values.extend(item.strip() for item in raw.split(","))
        return ClientCapabilities(capabilities=[item for item in values if item])

    async def _handle(self, websocket: ServerConnection) -> None:
        if not self._authorized(websocket):
            await websocket.close(code=1008, reason="unauthorized")
            return
        client = _WebSocketClient(websocket)
        await client.start()
        await self._session.connect(
            client,
            capabilities=self._capabilities_from_websocket(websocket),
        )
        try:
            async for message in websocket:
                await self._handle_message(client, str(message))
        finally:
            self._session.disconnect(client)
            await client.close()

    async def _send_json(self, client: _WebSocketClient, payload: dict[str, object]) -> None:
        await client.send_text(json.dumps(payload), priority=True)

    async def _handle_message(self, client: _WebSocketClient, raw: str) -> None:
        from voidx.presentation.protocol.v2.envelope import JsonRpcRequest, JsonRpcResult
        from voidx.presentation.protocol.requests import UiResponse

        try:
            msg = parse_jsonrpc_message_str(raw)
        except ParseError as exc:
            await self._send_json(client, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": exc.code, "message": exc.message},
            })
            return

        if isinstance(msg, JsonRpcRequest):
            result = await self._session.dispatch_request(msg, client=client)
            await client.send_text(result.model_dump_json(), priority=True)
        elif isinstance(msg, JsonRpcNotification):
            if msg.method == "snapshot.requested":
                thread_id = msg.params.get("thread_id")
                await self._session.request_snapshot(
                    client,
                    thread_id=thread_id if isinstance(thread_id, str) else "",
                )
            elif msg.method == "client.capabilities":
                self._session.set_client_capabilities(
                    client,
                    ClientCapabilities.model_validate(msg.params),
                )
        elif isinstance(msg, JsonRpcResult):
            result = msg.result if isinstance(msg.result, dict) else {}
            response = UiResponse(
                request_id=str(msg.id),
                value=result.get("value"),
            )
            await self._session.handle_response(response, thread_id=str(result.get("thread_id") or ""))

    def _authorized(self, websocket: ServerConnection) -> bool:
        if not self._token:
            return True
        request = getattr(websocket, "request", None)
        path = getattr(request, "path", "")
        query = parse_qs(urlparse(path).query)
        return query.get("token") == [self._token]


def parse_jsonrpc_message_str(message: str) -> JsonRpcRequest | JsonRpcNotification | JsonRpcResult | JsonRpcError:
    """Parse a raw JSON string into a v2 JSON-RPC message."""

    try:
        raw = json.loads(message)
    except json.JSONDecodeError:
        from voidx.presentation.protocol.v2.envelope import ERR_PARSE_ERROR
        raise ParseError(ERR_PARSE_ERROR, "parse error: invalid JSON")
    return parse_jsonrpc_message(raw)
