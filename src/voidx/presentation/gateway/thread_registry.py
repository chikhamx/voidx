"""Gateway thread registration adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from voidx.agent.ports.presentation import SessionPresentationStatus


class GatewayThreadSession(Protocol):
    def has_thread(self, thread_id: str) -> bool: ...
    async def register_thread(
        self,
        thread_id: str,
        *,
        title: str = "",
        directory: str = "",
        runtime_profile: str = "coding",
        profile_snapshot: object | None = None,
    ) -> None: ...


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
                    runtime_profile=session.runtime_profile,
                    profile_snapshot=session.profile_snapshot,
                )
            )
    def resolved_profile(self, thread_id: str) -> object | None:
        gateway_session = self._session_provider()
        if gateway_session is None:
            return None
        return gateway_session.resolved_profile(thread_id)
