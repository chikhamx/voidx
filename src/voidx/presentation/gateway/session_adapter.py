"""Gateway session construction helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from voidx.agent.ports.persistence import SessionRepository
from voidx.agent.ports.presentation import RuntimePresentationStatus, RuntimeStatusReader
from voidx.presentation.gateway import GatewaySession
from voidx.presentation.output.tree import OutputTree


class GatewayDock(Protocol):
    tree: OutputTree


SettingsFactory = Callable[[str], Awaitable[object]]
SkillsApiFactory = Callable[[str], Awaitable[object]]
SkillsApiProvider = Callable[[str], object]


def build_gateway_session(
    status_reader: RuntimeStatusReader,
    active_dock: GatewayDock,
    *,
    settings_factory: SettingsFactory | None = None,
    skills_api_factory: SkillsApiFactory | None = None,
    skills_api_provider: SkillsApiProvider | None = None,
    settings_update_handler: Callable[[object], Awaitable[None] | None] | None = None,
    usage_stats_provider: Callable[[], object] | None = None,
    mcp_catalog_provider: Callable[[], list] | None = None,
    session_repository: SessionRepository | None = None,
) -> GatewaySession:
    status = status_reader.runtime_status()
    session_id = status.session.session_id
    return GatewaySession(
        lambda: active_dock.tree,
        thread_id=session_id,
        runtime_profile=status.session.runtime_profile,
        session_id=session_id,
        workspace=status.workspace,
        settings_factory=settings_factory,
        skills_api_factory=skills_api_factory,
        skills_api_provider=skills_api_provider,
        runtime_state_provider=lambda: _runtime_state(status_reader.runtime_status()),
        settings_update_handler=settings_update_handler,
        usage_stats_provider=usage_stats_provider,
        mcp_catalog_provider=mcp_catalog_provider,
        session_repository=session_repository,
    )


def _runtime_state(status: RuntimePresentationStatus) -> dict[str, object]:
    return {
        "provider": status.provider,
        "model": status.model,
        "workspace": status.workspace,
        "profile_configured": status.profile_configured,
        "runtime_profile": status.session.runtime_profile,
        "permission_mode": status.permission_mode,
        "ai_approval_count": status.ai_approval_count,
    }
