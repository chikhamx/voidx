"""UiEventBus consumer that mirrors events to a GatewaySession."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.presentation.output.events.schema import UiEvent

if TYPE_CHECKING:
    from voidx.presentation.gateway.session.core import GatewaySession


class GatewayEventConsumer:
    """UiEventBus consumer that mirrors events to a GatewaySession."""

    def __init__(self, session: GatewaySession) -> None:
        self._session = session

    async def handle(self, event: UiEvent) -> None:
        await self._session.broadcast_event(event)
        if event.kind == "refresh.requested":
            await self._session.broadcast_snapshot()
