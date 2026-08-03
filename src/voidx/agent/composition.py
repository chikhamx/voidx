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
from voidx.agent.application.goal_service import GoalService
from voidx.agent.goal.evaluator import GoalEvaluator
from voidx.agent.goal.scheduler import GoalRuntimeScheduler
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


def _make_goal_result_notifier():
    """Persist a goal's terminal result into the parent (host) session."""
    import asyncio

    def _notify(parent_thread_id: str, text: str) -> None:
        async def _save() -> None:
            from voidx.memory.service import MessageRow, memory_now, save_message

            try:
                await save_message(
                    MessageRow(
                        session_id=parent_thread_id,
                        role="assistant",
                        content=text,
                        content_format="text",
                        created_at=memory_now(),
                    )
                )
            except Exception:
                pass  # best-effort; /goal status remains authoritative

        asyncio.get_running_loop().create_task(_save())

    return _notify


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
    goal_service = None
    model = getattr(execution, "model", None)
    if model is not None:
        goal_scheduler = GoalRuntimeScheduler(
            store=loop_store,
            runtime=runtime,
            workspace=getattr(config, "workspace", ""),
            evaluator=GoalEvaluator(),
        )
        goal_service = GoalService(
            store=loop_store,
            scheduler=goal_scheduler,
            workspace=getattr(config, "workspace", ""),
            result_notifier=_make_goal_result_notifier(),
        )
    if hasattr(execution, "__dict__"):
        execution.loop_service = loop_service
        execution.goal_service = goal_service
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
