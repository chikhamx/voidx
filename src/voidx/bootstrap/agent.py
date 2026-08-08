"""Top-level composition for the agent application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from voidx.agent.adapters.persistence.parent_result_publisher import AsyncParentResultPublisher
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
from voidx.agent.facade import AgentFacade
from voidx.agent.infrastructure.input_adapter import LangGraphInputAdapter
from voidx.agent.infrastructure.input_router import LangGraphAutonomousInputRouter
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher
from voidx.agent.infrastructure.presentation_adapter import (
    LangGraphPresentationBinding,
    LangGraphPresentationIntegrations,
    LangGraphRuntimeStatusReader,
    LangGraphSessionLifecycle,
)
from voidx.agent.ports.presentation import AgentEventPublisher, NullAgentEventPublisher
from voidx.agent.ports.ui import AgentUiPort
from voidx.agent.ports.workspace_lock import DelegatingWorkspaceWriteLock
from voidx.presentation.adapters.persistence.transcript_adapter import TranscriptSnapshotAdapter
from voidx.presentation.output.console import VoidConsole
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events import ui_events
from voidx.presentation.output.tool_events import PresentationToolUiEventPublisher
from voidx.presentation.runtime_port import PresentationUiAdapter
from voidx.presentation.session import session_tracker
from voidx.presentation.terminal.events import UiAgentEventPublisher
from voidx.presentation.terminal.run_loop import TerminalRunLoop

if TYPE_CHECKING:
    from voidx.agent.adapters.persistence.session_repository import SessionInfo
    from voidx.config import Config, Settings


AgentEventPublisherFactory = Callable[[Any], AgentEventPublisher]


@dataclass(frozen=True)
class ApplicationResources:
    turn_engine: LangGraphTurnEngine
    sessions: MemorySessionAdapter
    events: NullEventPublisher


@dataclass(frozen=True)
class IntegrationResources:
    external_manager_factory: Callable[..., tuple[Any, Any]] | None = None
    mcp_reference_resolver: Callable[..., Any] | None = None
    web_route: Callable[..., Any] | None = None
    permission_service_factory: Callable[..., Any] | None = None
    event_publisher_factory: AgentEventPublisherFactory | None = None
    parent_result_publisher_factory: Callable[[], AsyncParentResultPublisher] = AsyncParentResultPublisher


@dataclass(frozen=True)
class AgentResources:
    execution: Any
    service: AgentService
    workspace_write_lock: DelegatingWorkspaceWriteLock | None = None
    input_frontend_binder: LangGraphInputAdapter | None = None




def build_agent_components(
    config: Config,
    api_key: [redacted] | None,
    *,
    session: SessionInfo | None = None,
    settings: Settings | None = None,
    ui: AgentUiPort,
    event_publisher_factory: AgentEventPublisherFactory | None = None,
    external_manager_factory: Callable[..., tuple[Any, Any]] | None = None,
    mcp_reference_resolver: Callable[..., Any] | None = None,
    web_route: Callable[..., Any] | None = None,
    permission_service_factory: Callable[..., Any] | None = None,
) -> AgentResources:
    """Build presentation-neutral agent services and infrastructure."""
    integrations = IntegrationResources(
        external_manager_factory=external_manager_factory,
        mcp_reference_resolver=mcp_reference_resolver,
        web_route=web_route,
        permission_service_factory=permission_service_factory,
        event_publisher_factory=event_publisher_factory,
    )
    workspace_write_lock = DelegatingWorkspaceWriteLock()
    from voidx.bootstrap.providers import build_model_catalog
    from voidx.bootstrap.skills import build_skills_api_provider
    from voidx.update import service as update_service

    def model_catalog_factory(value):
        return build_model_catalog(value)

    skills_api_provider = build_skills_api_provider(config.workspace, settings)

    def skills_api_factory(value):
        return skills_api_provider.replace(config.workspace, value)

    execution_kwargs = {
        "session": session,
        "ui": ui,
        "workspace_write_lock": workspace_write_lock,
        "settings": settings,
        "model_catalog": model_catalog_factory(settings),
        "model_catalog_factory": model_catalog_factory,
        "skills_api": skills_api_provider(config.workspace),
        "skills_api_factory": skills_api_factory,
        "skills_api_provider": skills_api_provider,
        "external_manager_factory": integrations.external_manager_factory,
        "mcp_reference_resolver": integrations.mcp_reference_resolver,
        "web_route": integrations.web_route,
        "update_service": update_service,
    }
    if integrations.permission_service_factory is not None:
        execution_kwargs["permission_service_factory"] = integrations.permission_service_factory
    execution = LangGraphExecution(config, api_key, **execution_kwargs)
    application = ApplicationResources(
        turn_engine=LangGraphTurnEngine(execution),
        sessions=MemorySessionAdapter(),
        events=NullEventPublisher(),
    )
    runtime = AgentRuntime(application)
    store = ThreadStore()
    event_publisher = (
        integrations.event_publisher_factory(execution)
        if integrations.event_publisher_factory is not None
        else None
    )
    workspace = config.workspace
    loop_service = LoopService(
        store=store,
        scheduler=LoopRuntimeScheduler(
            store=store,
            runtime=runtime,
            workspace=workspace,
            session_id=(session.id if session is not None else ""),
            events=event_publisher,
        ),
        workspace=workspace,
        events=event_publisher,
    )
    goal_service = None
    if execution.model is not None:
        goal_service = GoalService(
            store=store,
            scheduler=GoalRuntimeScheduler(
                store=store,
                runtime=runtime,
                workspace=workspace,
                evaluator=GoalEvaluator(),
            ),
            workspace=workspace,
            result_publisher=integrations.parent_result_publisher_factory(),
        )
    execution.bind_automation_services(loop_service, goal_service)
    chat_service = ChatService(runtime)
    coding_service = CodingService(runtime)
    input_adapter = LangGraphInputAdapter(execution)
    router = LangGraphAutonomousInputRouter(
        execution,
        runtime,
        event_publisher or NullAgentEventPublisher(),
        execution,
        chat_service=chat_service,
        coding_service=coding_service,
        loop_service=loop_service,
        goal_service=goal_service,
    )
    service = AgentService(input_adapter, input_adapter, router, execution)
    execution.bind_coding_turn_runner(service.run_coding_turn)
    return AgentResources(
        execution=execution,
        service=service,
        workspace_write_lock=workspace_write_lock,
        input_frontend_binder=input_adapter,
    )


def build_agent_app(
    config: Config,
    api_key: [redacted] | None,
    *,
    session: SessionInfo | None = None,
    settings: Settings | None = None,
) -> AgentFacade:
    """Build the agent and its concrete terminal/web presentation."""
    from voidx.bootstrap.permission import build_permission_service
    from voidx.bootstrap.tooling import build_external_managers, resolve_mcp_references
    from voidx.tooling.adapters.web_mcp import call_mcp_web_tool

    presentation_ui = PresentationUiAdapter(
        output=VoidConsole(), dock=BottomInputDock(), events=ui_events, session_tracker=session_tracker
    )
    components = build_agent_components(
        config,
        api_key,
        session=session,
        settings=settings,
        ui=presentation_ui,
        event_publisher_factory=lambda execution: UiAgentEventPublisher(execution.ui),
        external_manager_factory=build_external_managers,
        mcp_reference_resolver=resolve_mcp_references,
        web_route=call_mcp_web_tool,
        permission_service_factory=build_permission_service,
    )
    components.execution.tool_ui_events = PresentationToolUiEventPublisher()
    components.execution.bind_presentation_snapshots(TranscriptSnapshotAdapter(presentation_ui))
    from voidx.bootstrap.application import build_settings
    skills_api_provider = components.execution.skills_api_provider

    async def workspace_skills_api_factory(workspace: str):
        return skills_api_provider(workspace)

    status_reader = LangGraphRuntimeStatusReader(components.execution)
    sessions = LangGraphSessionLifecycle(components.execution)
    integrations = LangGraphPresentationIntegrations(components.execution)
    frontend_binding = LangGraphPresentationBinding(components.execution, components.input_frontend_binder)
    run_loop = TerminalRunLoop(
        status_reader,
        sessions,
        integrations,
        frontend_binding,
        components.service,
        components.service,
        components.workspace_write_lock,
        presentation_ui,
        settings_factory=build_settings,
        skills_api_factory=workspace_skills_api_factory,
        skills_api_provider=skills_api_provider,
    )
    return AgentFacade(run_loop=run_loop)


__all__ = [
    "AgentEventPublisherFactory",
    "AgentResources",
    "ApplicationResources",
    "IntegrationResources",
    "build_agent_app",
    "build_agent_components",
]
