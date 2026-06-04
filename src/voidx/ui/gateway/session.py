"""Read-only protocol gateway session for web frontends."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol

from voidx.ui.output.events.schema import UiEvent
from voidx.ui.protocol import (
    UiCommand,
    UiEventEnvelope,
    UiRequest,
    UiRequestEnvelope,
    UiResponse,
    UiSnapshotEnvelope,
    tree_to_snapshot,
)
from voidx.ui.output.tree import OutputTree


class ProtocolClient(Protocol):
    async def send_text(self, text: str) -> None:
        """Send an encoded protocol envelope to the connected client."""


class GatewaySession:
    """Broadcasts transcript snapshots and UI events to read-only clients."""

    def __init__(
        self,
        tree_provider: Callable[[], OutputTree],
        *,
        session_id: str = "",
        command_handler: Callable[[UiCommand], Awaitable[None] | None] | None = None,
    ) -> None:
        self._tree_provider = tree_provider
        self._session_id = session_id
        self._command_handler = command_handler
        self._clients: set[ProtocolClient] = set()
        self._pending_requests: dict[str, asyncio.Future[UiResponse]] = {}
        self._seq = 0

    @property
    def clients(self) -> frozenset[ProtocolClient]:
        return frozenset(self._clients)

    async def connect(self, client: ProtocolClient) -> None:
        self._clients.add(client)
        try:
            await client.send_text(self._encode_snapshot())
        except Exception:
            self._clients.discard(client)
            raise

    def disconnect(self, client: ProtocolClient) -> None:
        self._clients.discard(client)

    def set_command_handler(
        self,
        handler: Callable[[UiCommand], Awaitable[None] | None] | None,
    ) -> None:
        self._command_handler = handler

    async def handle_command(self, command: UiCommand) -> None:
        if self._command_handler is None:
            return
        result = self._command_handler(command)
        if inspect.isawaitable(result):
            await result

    async def request(self, request: UiRequest) -> UiResponse | None:
        if not self._clients:
            return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[UiResponse] = loop.create_future()
        self._pending_requests[request.request_id] = future
        envelope = UiRequestEnvelope(seq=self._next_seq(), payload=request)
        try:
            await self._broadcast(envelope.model_dump_json())
            if not self._clients:
                return None
            return await future
        finally:
            self._pending_requests.pop(request.request_id, None)

    async def handle_response(self, response: UiResponse) -> None:
        future = self._pending_requests.pop(response.request_id, None)
        if future is not None and not future.done():
            future.set_result(response)

    async def broadcast_event(self, event: UiEvent) -> None:
        if not self._clients:
            return
        envelope = UiEventEnvelope(seq=self._next_seq(), payload=event)
        await self._broadcast(envelope.model_dump_json())

    async def broadcast_snapshot(self) -> None:
        if not self._clients:
            return
        await self._broadcast(self._encode_snapshot())

    def _encode_snapshot(self) -> str:
        seq = self._next_seq()
        snapshot = tree_to_snapshot(
            self._tree_provider(),
            session_id=self._session_id,
            revision=seq,
        )
        return UiSnapshotEnvelope(seq=seq, payload=snapshot).model_dump_json()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _broadcast(self, text: str) -> None:
        results = await asyncio.gather(
            *(client.send_text(text) for client in tuple(self._clients)),
            return_exceptions=True,
        )
        for client, result in zip(tuple(self._clients), results, strict=False):
            if isinstance(result, Exception):
                self._clients.discard(client)


class GatewayEventConsumer:
    """UiEventBus consumer that mirrors events to a GatewaySession."""

    def __init__(self, session: GatewaySession) -> None:
        self._session = session

    async def handle(self, event: UiEvent) -> None:
        await self._session.broadcast_event(event)
        if event.kind == "refresh.requested":
            await self._session.broadcast_snapshot()
