"""Narrow projections from LangGraph execution to presentation ports."""

from __future__ import annotations

from typing import Any

from voidx.agent.application.workflow_utils import active_workflow_names
from voidx.agent.domain.task.state import goal_label
from voidx.agent.ports.input import InputFrontend, InputFrontendBinder
from voidx.agent.ports.presentation import RuntimePresentationStatus, SessionPresentationStatus


class LangGraphRuntimeStatusReader:
    def __init__(self, host: Any) -> None:
        self._host = host

    def runtime_status(self) -> RuntimePresentationStatus:
        host = self._host
        session = host.session
        settings = host.settings
        model_config = host.config.model
        reasoning = model_config.reasoning_effort
        wall_clock = getattr(host.runtime_guards, "wall_clock", None)
        return RuntimePresentationStatus(
            provider=model_config.provider,
            model=model_config.model,
            workspace=host.workspace,
            profile_configured=host.model is not None,
            session=SessionPresentationStatus(
                session_id=host.session_id,
                title=session.title if session else "New session",
                directory=getattr(session, "directory", "") if session else "",
                runtime_profile=getattr(session, "runtime_profile", "coding") if session else "coding",
                is_new=session is None,
            ),
            permission_mode=getattr(host.permission, "permission_mode", ""),
            permission_label=host.permission.permission_mode_label(),
            ai_approval_count=getattr(host.permission, "ai_approval_count", 0),
            debug=host.debug_enabled,
            plan_mode=host.plan_mode,
            interaction_mode=host.interaction_mode.value,
            goal_label=goal_label(host.task_state.current_goal),
            active_workflows=tuple(active_workflow_names(host.task_state)),
            reasoning_effort=reasoning.value if reasoning is not None else "xhigh",
            protocol=model_config.protocol or "",
            context_window=model_config.context_window,
            mcp_config_path=str(settings.path) if settings is not None else "",
            code_ide=(settings.get_code_ide().value if settings is not None and callable(getattr(settings, "get_code_ide", None)) else "trae"),
            latest_action=getattr(wall_clock, "latest_action", ""),
        )


class LangGraphSessionLifecycle:
    def __init__(self, host: Any) -> None:
        self._host = host

    async def restore_runtime_state(self) -> None:
        await self._host.restore_runtime_state()

    async def delete_empty_current_session(self) -> None:
        await self._host.delete_empty_current_session()

    async def clear_current_session(self) -> None:
        await self._host.clear_current_session()


class LangGraphPresentationIntegrations:
    def __init__(self, host: Any) -> None:
        self._host = host

    async def close_agent_gateway(self) -> None:
        await self._host.agent_gateway.close_all()

    async def stop_integrations(self) -> None:
        if self._host.mcp_manager is not None:
            await self._host.mcp_manager.stop_all()
        if self._host.lsp_manager is not None:
            await self._host.lsp_manager.stop_all()

    async def initialize_lsp(self) -> list[Any]:
        manager = self._host.lsp_manager
        if manager is None:
            return []
        await manager.initialize()
        return list(manager.doctor())

    async def warm_up_lsp(self) -> dict[str, str]:
        manager = self._host.lsp_manager
        if manager is None:
            return {}
        warm_up = getattr(manager, "warm_up", None)
        return await warm_up() if callable(warm_up) else {}

    def has_lsp(self) -> bool:
        return self._host.lsp_manager is not None

    def enabled_mcp_names(self) -> tuple[str, ...]:
        settings = self._host.settings
        if settings is None:
            return ()
        return tuple(server.name for server in settings.list_mcp_servers() if not server.disabled)

    async def start_mcp(self) -> None:
        if self._host.mcp_manager is not None:
            await self._host.mcp_manager.start_all()

    def has_mcp(self) -> bool:
        return self._host.mcp_manager is not None

    def mcp_catalog(self) -> list[Any]:
        manager = self._host.mcp_manager
        return manager.catalog_snapshot() if manager is not None else []

    def mcp_statuses(self) -> list[Any]:
        manager = self._host.mcp_manager
        return manager.statuses() if manager is not None else []


class LangGraphPresentationBinding:
    def __init__(self, host: Any, input_frontend_binder: InputFrontendBinder) -> None:
        self._host = host
        self._input_frontend_binder = input_frontend_binder

    def bind_input_frontend(self, frontend: InputFrontend | None) -> None:
        self._input_frontend_binder.bind_frontend(frontend)

    def reset_run_state(self) -> None:
        self._host.any_messages_sent = False


    def bind_startup_presenter(self, presenter: Any) -> None:
        self._host.bind_startup_presenter(presenter)

    async def apply_settings_update(self, settings: object) -> None:
        await self._host.apply_settings_update(settings)

    def usage_stats(self) -> object:
        return self._host.usage_stats

    def update_check_due(self) -> bool:
        settings = self._host.settings
        return bool(settings is not None and settings.update_check_due())

    def mark_update_check(self, version: str | None) -> None:
        settings = self._host.settings
        if settings is not None:
            settings.mark_update_check(version)

    async def restore_transcript_snapshot(self, *, append: bool = False) -> bool:
        return await self._host.restore_transcript_snapshot(append=append)
