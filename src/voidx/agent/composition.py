"""Composition root for the agent application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.agent.application.agent_service import AgentService
from voidx.agent.application.chat_service import ChatService
from voidx.agent.application.coding_service import CodingService
from types import SimpleNamespace

from voidx.agent.runtime import AgentRuntime
from voidx.agent.loop.scheduler import LoopRuntimeScheduler
from voidx.agent.application.loop_service import LoopService
from voidx.config.ports import bind_model_profile_store
from voidx.memory.profile_store import MemoryModelProfileStore
from voidx.memory.service import ThreadStore
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
    bind_model_profile_store(MemoryModelProfileStore())

    execution = LangGraphExecution(config, api_key, session=session, settings=settings)
    engine = LangGraphTurnEngine(execution)
    sessions = MemorySessionAdapter()
    events = NullEventPublisher()
    runtime = AgentRuntime(
        SimpleNamespace(turn_engine=engine, sessions=sessions, events=events)
    )
    loop_store = ThreadStore()
    loop_scheduler = LoopRuntimeScheduler(
        store=loop_store,
        runtime=runtime,
        workspace=getattr(config, "workspace", ""),
        session_id=(session.id if session is not None else ""),
    )
    loop_service = LoopService(
        store=loop_store,
        scheduler=loop_scheduler,
        workspace=getattr(config, "workspace", ""),
    )
    if hasattr(execution, "__dict__"):
        execution.loop_service = loop_service
    chat_service = ChatService(runtime)
    coding_service = CodingService(runtime)
    return AgentFacade(
        AgentService(
            execution,
            runtime,
            chat_service=chat_service,
            coding_service=coding_service,
        )
    )
