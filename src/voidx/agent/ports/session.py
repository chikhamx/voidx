"""Session persistence port."""

from __future__ import annotations

from typing import Protocol

from voidx.agent.domain.state import SessionRuntimeState


class SessionStore(Protocol):
    async def load_runtime(self, session_id: str) -> SessionRuntimeState: ...

    async def save_runtime(self, session_id: str, runtime: SessionRuntimeState) -> None: ...

    async def clear_runtime(self, session_id: str) -> None: ...
