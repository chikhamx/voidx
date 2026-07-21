"""Composition root for the agent application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.agent.application.agent_service import AgentService
from types import SimpleNamespace

from voidx.agent.application.turn_service import TurnService
from voidx.agent.runtime import AgentRuntime
from voidx.agent.facade import AgentFacade
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher

if TYPE_CHECKING:
    from voidx.config import Config, Settings
    from voidx.memory.service import SessionInfo


def build_agent_app(
    config: Config,
    api_key: str | None,
    *,
    session: SessionInfo | None = None,
    settings: Settings | None = None,
) -> AgentFacade:
    """Build the agent application and its LangGraph infrastructure."""
    execution = LangGraphExecution(config, api_key, session=session, settings=settings)
    engine = LangGraphTurnEngine(execution)
    sessions = MemorySessionAdapter()
    events = NullEventPublisher()
    runtime = AgentRuntime(
        SimpleNamespace(turn_engine=engine, sessions=sessions, events=events)
    )
    return AgentFacade(AgentService(execution, runtime))
