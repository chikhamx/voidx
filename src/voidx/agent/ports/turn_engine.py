"""Single-turn execution port."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.agent.domain.state import AgentRuntime


class TurnEngine(Protocol):
    async def run(
        self,
        user_text: str,
        runtime: AgentRuntime,
        *,
        display_text: str | None = None,
        context: Any | None = None,
    ) -> AgentRuntime: ...
