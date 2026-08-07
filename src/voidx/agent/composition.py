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
from voidx.agent.infrastructure.input_adapter import LangGraphInputAdapter
from voidx.agent.infrastructure.input_router import LangGraphAutonomousInputRouter
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher
from voidx.agent.ports.presentation import AgentEventPublisher, NullAgentEventPublisher
from voidx.agent.ports.ui import AgentUiPort
from voidx.agent.ports.workspace_lock import DelegatingWorkspaceWriteLock

if TYPE_CHECKING:
    from voidx.agent.adapters.persistence.session_repository import SessionInfo
    from voidx.config import Config, Settings


AgentEventPublisherFactory = Callable[[Any], AgentEventPublisher]


@dataclass(frozen=True)
class AgentComponents:
    """Presentation-neutral objects needed by an outer composition root."""

    execution: Any
    service: AgentService
    workspace_write_lock: DelegatingWorkspaceWriteLock | None = None
    input_frontend_binder: LangGraphInputAdapter | None = None


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
    ui: AgentUiPort,
    event_publisher_factory: AgentEventPublisherFactory | None = None,
    external_manager_factory: Callable[..., tuple[Any, Any]] | None = None,
    mcp_reference_resolver: Callable[..., Any] | None = None,
    web_route: Callable[..., Any] | None = None,
    permission_service_factory: Callable[..., Any] | None = None,
) -> AgentComponents:
    """Build agent services and infrastructure without choosing a presentation."""

    workspace_write_lock = DelegatingWorkspaceWriteLock()
    execution = LangGraphExecution(
        config,
        api_key,
        session=session,
        ui=ui,
        workspace_write_lock=workspace_write_lock,
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
    event_publisher = (
        event_publisher_factory(execution)
        if event_publisher_factory is not None
        else None
    )
    loop_scheduler = LoopRuntimeScheduler(
        store=loop_store,
        runtime=runtime,
        workspace=getattr(config, "workspace", ""),
        session_id=(session.id if session is not None else ""),
        events=event_publisher,
    )
    loop_service = LoopService(
        store=loop_store,
        scheduler=loop_scheduler,
        workspace=getattr(config, "workspace", ""),
        events=event_publisher,
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
    input_adapter = LangGraphInputAdapter(execution)
    guidance = execution
    autonomous_router = LangGraphAutonomousInputRouter(
        execution,
        runtime,
        event_publisher or NullAgentEventPublisher(),
        guidance,
    )
    autonomous_router.bind_turn_services(
        chat_service=chat_service,
        coding_service=coding_service,
    )
    service = AgentService(input_adapter, input_adapter, autonomous_router, guidance)
    execution.bind_coding_turn_runner(service.run_coding_turn)
    return AgentComponents(
        execution=execution,
        service=service,
        workspace_write_lock=workspace_write_lock,
        input_frontend_binder=input_adapter,
    )


__all__ = ["AgentComponents", "AgentEventPublisherFactory", "build_agent_components"]
