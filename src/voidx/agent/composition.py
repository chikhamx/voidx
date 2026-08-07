"""Composition of presentation-neutral agent application resources."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable

from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.application.agent_service import AgentService
from voidx.agent.application.automation.goal.evaluator import GoalEvaluator
from voidx.agent.application.automation.goal.goal_service import GoalService
from voidx.agent.application.automation.goal.scheduler import GoalRuntimeScheduler
from voidx.agent.application.automation.loop.loop_service import LoopService
from voidx.agent.application.automation.loop.scheduler import LoopRuntimeScheduler
from voidx.agent.application.chat_service import ChatService
from voidx.agent.application.coding_service import CodingService
from voidx.agent.application.runtime import AgentRuntime
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher
from voidx.agent.ports.presentation import AgentEventPublisher

if TYPE_CHECKING:
    from voidx.agent.adapters.persistence.session_repository import SessionInfo
    from voidx.config import Config, Settings


AgentEventPublisherFactory = Callable[[Any], AgentEventPublisher]


@dataclass(frozen=True)
class AgentComponents:
    """Presentation-neutral objects needed by an outer composition root."""

    execution: Any
    service: AgentService


def _make_goal_result_notifier():
    """Persist a goal's terminal result into the parent (host) session."""
    import asyncio

    def _notify(parent_thread_id: str, text: str) -> None:
        async def _save() -> None:
            from voidx.agent.adapters.persistence.session_repository import MessageRow, save_message
            from voidx.persistence.sqlite import now as memory_now

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


def build_agent_components(
    config: Config,
    api_key: str | None,
    *,
    session: SessionInfo | None = None,
    settings: Settings | None = None,
    event_publisher_factory: AgentEventPublisherFactory | None = None,
    external_manager_factory: Callable[..., tuple[Any, Any]] | None = None,
    mcp_reference_resolver: Callable[..., Any] | None = None,
    web_route: Callable[..., Any] | None = None,
    permission_service_factory: Callable[..., Any] | None = None,
) -> AgentComponents:
    """Build agent services and infrastructure without choosing a presentation."""

    execution = LangGraphExecution(
        config,
        api_key,
        session=session,
        settings=settings,
        external_manager_factory=external_manager_factory,
        mcp_reference_resolver=mcp_reference_resolver,
        web_route=web_route,
        **({"permission_service_factory": permission_service_factory} if permission_service_factory is not None else {}),
    )
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
    event_publisher = (
        event_publisher_factory(execution)
        if event_publisher_factory is not None
        else None
    )
    service = AgentService(
        execution,
        runtime,
        chat_service=chat_service,
        coding_service=coding_service,
        events=event_publisher,
    )
    return AgentComponents(execution=execution, service=service)


__all__ = ["AgentComponents", "AgentEventPublisherFactory", "build_agent_components"]
