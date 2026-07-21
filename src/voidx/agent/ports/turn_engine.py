"""Single-turn execution port."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.agent.domain.state import SessionRuntimeState


class TurnEngine(Protocol):
    async def run(
        self,
        user_text: str,
        runtime: SessionRuntimeState,
        *,
        display_text: str | None = None,
        context: Any | None = None,
    ) -> SessionRuntimeState: ...
