"""Narrow adapters from LangGraph execution capabilities to slash use cases."""

from __future__ import annotations


class _PromptUi:
    def __init__(self, execution): self._execution = execution
    @property
    def status(self): return self._execution.presentation_ui._status_frontend.status
    async def ask_choice(self, prompt, choices, **kwargs): return await self._execution.presentation_ui.ask_choice(prompt, choices, **kwargs)
    async def ask_text(self, prompt, **kwargs): return await self._execution.presentation_ui.ask_text(prompt, **kwargs)


class _UiState:
    def __init__(self, execution): self._execution = execution
    @property
    def session_tracker(self): return self._execution.presentation_ui.session_tracker
    def get_dock(self): return self._execution.presentation_ui.get_dock()



class _LogConfigView:
    def __init__(self, execution): self._execution = execution
    @property
    def log_llm_exchange(self): return self._execution.config.log_llm_exchange
    @log_llm_exchange.setter
    def log_llm_exchange(self, value): self._execution.config.log_llm_exchange = value
    @property
    def log_llm_diagnostic(self): return self._execution.config.log_llm_diagnostic
    @log_llm_diagnostic.setter
    def log_llm_diagnostic(self, value): self._execution.config.log_llm_diagnostic = value

class _ModelConfigView:
    def __init__(self, execution): self._execution = execution
    @property
    def model(self): return self._execution.config.model


class _UserConfigView:
    def __init__(self, execution): self._execution = execution
    @property
    def user_profile(self): return self._execution.config.user_profile
    @user_profile.setter
    def user_profile(self, value): self._execution.config.user_profile = value


class _ModelSettingsOps:
    def __init__(self, execution): self._execution = execution
    async def list_profiles(self): return await self._execution.settings.list_profiles()
    async def resolve_api_key(self, provider): return await self._execution.settings.resolve_api_key(provider)
    async def resolve_base_url(self, provider): return await self._execution.settings.resolve_base_url(provider)
    async def resolve_profile(self, name=None): return await self._execution.settings.resolve_profile(name)
    async def resolve_protocol(self, provider): return await self._execution.settings.resolve_protocol(provider)
    async def save_profile(self, profile, **kwargs): return await self._execution.settings.save_profile(profile, **kwargs)
    async def delete_profile(self, name, **kwargs): return await self._execution.settings.delete_profile(name, **kwargs)
    def list_custom_providers(self): return self._execution.settings.list_custom_providers()
    def _set_setting(self, name, value): return self._execution.settings._set_setting(name, value)
    def _pop_setting(self, name): return self._execution.settings._pop_setting(name)


class _IntegrationSettingsOps:
    def __init__(self, execution): self._execution = execution
    def get_tavily_api_key(self): return self._execution.settings.get_tavily_api_key()
    def get_mcp_server(self, name): return self._execution.settings.get_mcp_server(name)
    def list_mcp_servers(self): return self._execution.settings.list_mcp_servers()
    def delete_mcp_server(self, name): return self._execution.settings.delete_mcp_server(name)
    def set_mcp_server_auto(self, name, enabled): return self._execution.settings.set_mcp_server_auto(name, enabled)
    def set_mcp_server_disabled(self, name, disabled): return self._execution.settings.set_mcp_server_disabled(name, disabled)
    def set_skill_auto(self, name, enabled): return self._execution.settings.set_skill_auto(name, enabled)
    def set_skill_enabled(self, name, enabled): return self._execution.settings.set_skill_enabled(name, enabled)
    def save_mcp_server(self, server): return self._execution.settings.save_mcp_server(server)
    def set_tavily_api_key(self, value): return self._execution.settings.set_tavily_api_key(value)
    def delete_tavily_api_key(self): return self._execution.settings.delete_tavily_api_key()
    def set_web_tool_route(self, kind, route): return self._execution.settings.set_web_tool_route(kind, route)
    def clear_web_routes_for_server(self, name, **kwargs): return self._execution.settings.clear_web_routes_for_server(name, **kwargs)


class _PreferenceSettingsOps:
    def __init__(self, execution): self._execution = execution
    def get_code_ide(self): return self._execution.settings.get_code_ide()
    def set_code_ide(self, value): return self._execution.settings.set_code_ide(value)
    async def list_profiles(self): return await self._execution.settings.list_profiles()
    def set_ai_approval_profile(self, value): return self._execution.settings.set_ai_approval_profile(value)
    def set_permission_mode(self, value): return self._execution.settings.set_permission_mode(value)
    def get_user_profile(self): return self._execution.settings.get_user_profile()
    def set_user_language(self, value): return self._execution.settings.set_user_language(value)
    def set_user_tone(self, value): return self._execution.settings.set_user_tone(value)
    def get_update_check_enabled(self): return self._execution.settings.get_update_check_enabled()
    def get_update_check_last_checked_at(self): return self._execution.settings.get_update_check_last_checked_at()
    def get_update_check_latest_version(self): return self._execution.settings.get_update_check_latest_version()
    def set_update_check_enabled(self, value): return self._execution.settings.set_update_check_enabled(value)
    def mark_update_check(self, version): return self._execution.settings.mark_update_check(version)
    def update_check_due(self): return self._execution.settings.update_check_due()


class _PermissionOps:
    def __init__(self, execution): self._execution = execution
    @property
    def permission_mode(self): return self._execution.permission.permission_mode
    def allow(self, pattern): return self._execution.permission.allow(pattern)
    def deny(self, pattern): return self._execution.permission.deny(pattern)
    def set_permission_mode(self, mode): return self._execution.permission.set_permission_mode(mode)
    def show_rules(self): return self._execution.permission.show_rules()


class _LspOps:
    def __init__(self, execution): self._execution = execution
    @property
    def initializing(self): return self._execution.lsp_manager.initializing
    @property
    def initialized(self): return self._execution.lsp_manager.initialized
    @property
    def servers(self): return self._execution.lsp_manager.servers
    def statuses(self): return self._execution.lsp_manager.statuses()
    def doctor(self): return self._execution.lsp_manager.doctor()
    async def restart(self, language=None): return await self._execution.lsp_manager.restart(language)


class _McpOps:
    def __init__(self, execution): self._execution = execution
    @property
    def started(self): return self._execution.mcp_manager.started
    def statuses(self): return self._execution.mcp_manager.statuses()
    async def restart_all(self): return await self._execution.mcp_manager.restart_all()
    async def list_tools_for_server(self, name): return await self._execution.mcp_manager.list_tools_for_server(name)


class _ModelCatalogOps:
    def __init__(self, execution): self._execution = execution
    async def list_models(self, provider): return await self._execution.model_catalog.list_models(provider)
    async def list_models_for_config(self, *args, **kwargs): return await self._execution.model_catalog.list_models_for_config(*args, **kwargs)
    def list_fallback_models(self, provider, **kwargs): return self._execution.model_catalog.list_fallback_models(provider, **kwargs)


class _SkillsOps:
    def __init__(self, execution): self._execution = execution
    @property
    def service(self): return self._execution.skills_api.service


class _UpdateOps:
    def __init__(self, execution): self._execution = execution
    async def check_for_update(self): return await self._execution.update_service.check_for_update()
    def is_newer(self, latest): return self._execution.update_service.is_newer(latest)
    async def perform_upgrade(self, version=None):
        if version is None: return await self._execution.update_service.perform_upgrade()
        return await self._execution.update_service.perform_upgrade(version)
    def upgrade_hint(self): return self._execution.update_service.upgrade_hint()


class SlashControlAdapter:
    def __init__(self, execution): self._execution = execution
    @property
    def ui(self): return self._execution.ui
    @property
    def prompt_ui(self): return _PromptUi(self._execution) if self._execution.presentation_ui._interaction_frontend is not None else None
    @property
    def permission_ops(self): return _PermissionOps(self._execution)
    async def compact_session_history(self, *, force=False): return await self._execution.compact_session_history(force=force)
    async def persist_runtime_state(self): return await self._execution.persist_runtime_state()


class AutomationSlashAdapter:
    def __init__(self, execution): self._execution = execution
    @property
    def ui(self): return self._execution.ui
    @property
    def goal_service(self): return self._execution.goal_service
    @property
    def loop_service(self): return self._execution.loop_service
    @property
    def session(self): return self._execution.session
    @property
    def workspace(self): return self._execution.workspace
    def can_submit_guidance(self): return self._execution.can_submit_guidance()
    def submit_guidance(self, text, **kwargs): return self._execution.submit_guidance(text, **kwargs)
    def interaction_mode_value(self): return self._execution.interaction_mode_value()
    async def run_coding_turn(self, text, *, display_text=None): return await self._execution.run_coding_turn(text, display_text=display_text)


class ModeSlashAdapter:
    def __init__(self, execution): self._execution = execution
    @property
    def ui(self): return self._execution.ui
    @property
    def ui_state(self): return _UiState(self._execution)
    @property
    def clipboard_image(self): return self._execution.clipboard_image
    @property
    def goal_service(self): return self._execution.goal_service
    @property
    def session(self): return self._execution.session
    @property
    def usage_stats(self): return self._execution.usage_stats
    @property
    def workspace(self): return self._execution.workspace
    @property
    def log_config(self): return _LogConfigView(self._execution)
    async def clear_current_session(self): return await self._execution.clear_current_session()
    def debug_enabled(self): return self._execution.debug_enabled
    def interaction_mode_value(self): return self._execution.interaction_mode_value()
    def set_debug(self, value): return self._execution.set_debug(value)
    def set_interaction_mode(self, mode): return self._execution.set_interaction_mode(mode)


class SessionSlashAdapter:
    def __init__(self, execution): self._execution = execution
    @property
    def ui(self): return self._execution.ui
    @property
    def ui_state(self): return _UiState(self._execution)
    @property
    def prompt_ui(self): return _PromptUi(self._execution) if self._execution.presentation_ui._interaction_frontend is not None else None
    @property
    def model_config(self): return _ModelConfigView(self._execution)
    @property
    def session(self): return self._execution.session
    @property
    def workspace(self): return self._execution.workspace
    async def regenerate_session_title(self): return await self._execution.regenerate_session_title()
    async def restore_transcript_snapshot(self, *, append=False): return await self._execution.restore_transcript_snapshot(append=append)
    async def resume_session(self, session): return await self._execution.resume_session(session)
    async def set_session_title(self, title): return await self._execution.set_session_title(title)
    async def show_startup(self, *, prefer_direct=False): return await self._execution.show_startup(prefer_direct=prefer_direct)


class ModelSlashAdapter:
    def __init__(self, execution): self._execution = execution
    @property
    def ui(self): return self._execution.ui
    @property
    def prompt_ui(self): return _PromptUi(self._execution) if self._execution.presentation_ui._interaction_frontend is not None else None
    @property
    def api_key(self): return self._execution.api_key
    @api_key.setter
    def api_key(self, value): self._execution.api_key = value
    @property
    def compaction(self): return self._execution.compaction
    @property
    def model_config(self): return _ModelConfigView(self._execution)
    @property
    def model(self): return self._execution.model
    @model.setter
    def model(self, value): self._execution.model = value
    @property
    def model_catalog_ops(self): return _ModelCatalogOps(self._execution)
    @property
    def provider_specs(self): return self._execution.provider_specs
    @property
    def reasoning_effort_type(self): return self._execution.reasoning_effort_type
    @property
    def session(self): return self._execution.session
    @property
    def model_settings(self): return _ModelSettingsOps(self._execution)
    @property
    def usage_stats(self): return self._execution.usage_stats
    def context_limit_resolver(self, provider, protocol, context_window): return self._execution.context_limit_resolver(provider, protocol, context_window)
    def model_factory(self, api_key, model_config): return self._execution.model_factory(api_key, model_config)


class IntegrationsSlashAdapter:
    def __init__(self, execution): self._execution = execution
    @property
    def ui(self): return self._execution.ui
    @property
    def prompt_ui(self): return _PromptUi(self._execution) if self._execution.presentation_ui._interaction_frontend is not None else None
    @property
    def lsp_ops(self): return _LspOps(self._execution) if self._execution.lsp_manager is not None else None
    @property
    def mcp_ops(self): return _McpOps(self._execution) if self._execution.mcp_manager is not None else None
    @property
    def integration_settings(self): return _IntegrationSettingsOps(self._execution)
    @property
    def skills_ops(self): return _SkillsOps(self._execution)
    @property
    def workspace(self): return self._execution.workspace
    def invalidate_skill_service_cache(self): return self._execution.invalidate_skill_service_cache()


class PreferencesSlashAdapter:
    def __init__(self, execution): self._execution = execution
    @property
    def ui(self): return self._execution.ui
    @property
    def prompt_ui(self): return _PromptUi(self._execution) if self._execution.presentation_ui._interaction_frontend is not None else None
    @property
    def user_config(self): return _UserConfigView(self._execution)
    @property
    def language_labels(self): return self._execution.language_labels
    @property
    def permission_ops(self): return _PermissionOps(self._execution)
    @property
    def preference_settings(self): return _PreferenceSettingsOps(self._execution) if self._execution.settings is not None else None
    @property
    def tone_labels(self): return self._execution.tone_labels
    @property
    def update_ops(self): return _UpdateOps(self._execution)
    def clear_successful_dangerous_calls(self): return self._execution.clear_successful_dangerous_calls()


def build_slash_ports(execution):
    return (
        SlashControlAdapter(execution),
        AutomationSlashAdapter(execution),
        ModeSlashAdapter(execution),
        SessionSlashAdapter(execution),
        ModelSlashAdapter(execution),
        IntegrationsSlashAdapter(execution),
        PreferencesSlashAdapter(execution),
    )
