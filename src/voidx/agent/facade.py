"""Public facade for running the composed agent application."""

from __future__ import annotations

from typing import Protocol

from voidx.agent.application.agent_service import RunLoopStartupError


class AgentRunLoop(Protocol):
    async def run(self, **kwargs: object) -> None: ...


class AgentFacade:
    """Stable application boundary with presentation supplied separately."""

    def __init__(self, *, run_loop: AgentRunLoop) -> None:
        self._run_loop = run_loop

    async def run(self, **kwargs: object) -> None:
        await self._run_loop.run(**kwargs)


__all__ = ["AgentFacade", "RunLoopStartupError"]
