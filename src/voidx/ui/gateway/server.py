"""WebSocket server wrapper for the v2 JSON-RPC gateway."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import Server, ServerConnection, serve

from voidx.ui.gateway.session import GatewaySession
from voidx.ui.protocol.v2.envelope import ParseError, parse_jsonrpc_message


class _WebSocketClient:
    def __init__(self, websocket: ServerConnection) -> None:
        self._websocket = websocket

    async def send_text(self, text: str) -> None:
        await self._websocket.send(text)


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
        self._server = await serve(self._handle, self._host, self._port)
        socket = self._server.sockets[0]
        self._bound_port = int(socket.getsockname()[1])

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        self._bound_port = None

    async def _handle(self, websocket: ServerConnection) -> None:
        if not self._authorized(websocket):
            await websocket.close(code=1008, reason="unauthorized")
            return
        client = _WebSocketClient(websocket)
        await self._session.connect(client)
        try:
            async for message in websocket:
                await self._handle_message(websocket, str(message))
        finally:
            self._session.disconnect(client)

    async def _handle_message(self, websocket: ServerConnection, raw: str) -> None:
        import json

        from voidx.ui.protocol.v2.envelope import JsonRpcRequest, JsonRpcResult
        from voidx.ui.protocol.requests import UiResponse

        try:
            msg = parse_jsonrpc_message_str(raw)
        except ParseError as exc:
            await websocket.send(json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": exc.code, "message": exc.message},
            }))
            return

        if isinstance(msg, JsonRpcRequest):
            result = await self._session.dispatch_request(msg)
            await websocket.send(result.model_dump_json())
        elif isinstance(msg, JsonRpcResult):
            response = UiResponse(
                request_id=str(msg.id),
                value=msg.result.get("value") if isinstance(msg.result, dict) else None,
            )
            await self._session.handle_response(response)

    def _authorized(self, websocket: ServerConnection) -> bool:
        if not self._token:
            return True
        request = getattr(websocket, "request", None)
        path = getattr(request, "path", "")
        query = parse_qs(urlparse(path).query)
        return query.get("token") == [self._token]


def parse_jsonrpc_message_str(message: str):
    """Parse a raw JSON string into a v2 JSON-RPC message."""
    import json

    try:
        raw = json.loads(message)
    except json.JSONDecodeError:
        from voidx.ui.protocol.v2.envelope import ERR_PARSE_ERROR
        raise ParseError(ERR_PARSE_ERROR, "parse error: invalid JSON")
    return parse_jsonrpc_message(raw)
