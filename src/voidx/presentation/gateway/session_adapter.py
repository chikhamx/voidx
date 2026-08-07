"""Gateway session construction helpers."""

from __future__ import annotations

from typing import Any

from voidx.runtime.ui import GatewaySession


def build_gateway_session(
    execution: Any,
    active_dock: Any,
    *,
    settings_factory: Any = None,
) -> Any:
    """Create the web gateway session for the active dock."""
    return GatewaySession(
        lambda: active_dock.tree,
        thread_id=execution.session.id if execution.session else "",
        session_id=execution.session.id if execution.session else "",
        workspace=execution.workspace,
        settings_factory=settings_factory,
        runtime_state_provider=lambda: {
            "provider": execution.config.model.provider,
            "model": execution.config.model.model,
            "workspace": execution.workspace,
            "profile_configured": execution.model is not None,
            "permission_mode": getattr(execution.permission, "permission_mode", ""),
            "ai_approval_count": getattr(execution.permission, "ai_approval_count", 0),
        },
        settings_update_handler=execution.apply_settings_update,
        usage_stats_provider=lambda: execution.usage_stats,
        mcp_catalog_provider=lambda: (
            execution.mcp_manager.catalog_snapshot()
            if execution.mcp_manager is not None
            else []
        ),
    )
