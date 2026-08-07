"""Top-level composition for the interactive agent application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.agent.composition import build_agent_components
from voidx.agent.facade import AgentFacade
from voidx.presentation.terminal.events import UiAgentEventPublisher
from voidx.presentation.output.tool_events import PresentationToolUiEventPublisher
from voidx.presentation.transcript_adapter import TranscriptSnapshotAdapter
from voidx.presentation.terminal.run_loop import TerminalRunLoop

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

    components = build_agent_components(
        config,
        api_key,
        session=session,
        settings=settings,
        event_publisher_factory=lambda execution: UiAgentEventPublisher(execution.ui),
        external_manager_factory=build_external_managers,
        mcp_reference_resolver=resolve_mcp_references,
        web_route=call_mcp_web_tool,
        permission_service_factory=build_permission_service,
    )
    components.execution.tool_ui_events = PresentationToolUiEventPublisher()
    components.execution.bind_presentation_snapshots(
        TranscriptSnapshotAdapter(components.execution.ui)
    )
    from voidx.bootstrap.application import build_settings

    run_loop = TerminalRunLoop(
        components.execution,
        components.service,
        settings_factory=build_settings,
    )
    return AgentFacade(components.service, run_loop=run_loop)


__all__ = ["build_agent_app"]
