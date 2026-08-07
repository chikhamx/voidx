"""Gateway thread registration adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from voidx.agent.ports.presentation import SessionPresentationStatus


class GatewayThreadSession(Protocol):
    def has_thread(self, thread_id: str) -> bool: ...
    async def register_thread(self, thread_id: str, *, title: str = "", directory: str = "") -> None: ...


class GatewayThreadRegistryAdapter:
    def __init__(self, session_provider: Callable[[], GatewayThreadSession | None]) -> None:
        self._session_provider = session_provider

    def ensure_thread(self, session: SessionPresentationStatus) -> None:
        gateway_session = self._session_provider()
        if gateway_session is None or session.is_new or not session.session_id:
            return
        if not gateway_session.has_thread(session.session_id):
            asyncio.ensure_future(
                gateway_session.register_thread(
                    session.session_id,
                    title=session.title,
                    directory=session.directory,
                )
            )
