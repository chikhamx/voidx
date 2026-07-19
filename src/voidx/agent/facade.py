"""Public facade for running the composed agent application."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.agent.application.agent_service import RunLoopStartupError


class AgentExecution(Protocol):
    """Execution object required by the agent facade."""

    async def run(self, **kwargs: Any) -> None: ...


class AgentFacade:
    """Stable application boundary around the current agent execution engine."""

    def __init__(self, execution: AgentExecution) -> None:
        self._execution = execution

    async def run(self, **kwargs: Any) -> None:
        await self._execution.run(**kwargs)
