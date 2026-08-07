"""Top-level composition for the interactive agent application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.agent.composition import build_agent_components
from voidx.agent.facade import AgentFacade
from voidx.agent.infrastructure.presentation_adapter import (
    LangGraphPresentationBinding,
    LangGraphPresentationIntegrations,
    LangGraphRuntimeStatusReader,
    LangGraphSessionLifecycle,
)
from voidx.presentation.terminal.events import UiAgentEventPublisher
from voidx.presentation.output.tool_events import PresentationToolUiEventPublisher
from voidx.presentation.adapters.persistence.transcript_adapter import TranscriptSnapshotAdapter
from voidx.presentation.terminal.run_loop import TerminalRunLoop
from voidx.presentation.output.console import VoidConsole
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events import ui_events
from voidx.presentation.runtime_port import PresentationUiAdapter
from voidx.presentation.session import session_tracker

if TYPE_CHECKING:
    from voidx.agent.adapters.persistence.session_repository import SessionInfo
    from voidx.config import Config, Settings


def build_agent_app(
    config: Config,
    api_key: str | None,
    *,
    session: SessionInfo | None = None,
    settings: Settings | None = None,
) -> AgentFacade:
    """Build the agent and its concrete terminal/web presentation."""
    from voidx.bootstrap.permission import build_permission_service
    from voidx.bootstrap.tooling import build_external_managers, resolve_mcp_references
    from voidx.tooling.adapters.web_mcp import call_mcp_web_tool

    presentation_ui = PresentationUiAdapter(
        output=VoidConsole(),
        dock=BottomInputDock(),
        events=ui_events,
        session_tracker=session_tracker,
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
    components.execution.bind_presentation_snapshots(
        TranscriptSnapshotAdapter(presentation_ui)
    )
    from voidx.bootstrap.application import build_settings

    status_reader = LangGraphRuntimeStatusReader(components.execution)
    sessions = LangGraphSessionLifecycle(components.execution)
    integrations = LangGraphPresentationIntegrations(components.execution)
    frontend_binding = LangGraphPresentationBinding(
        components.execution,
        components.input_frontend_binder,
    )
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
    )
    return AgentFacade(components.service, run_loop=run_loop)


__all__ = ["build_agent_app"]
