"""Settings JSON-RPC method handlers for GatewaySession."""

from __future__ import annotations

from voidx.presentation.protocol.v2.methods import MethodParamsError



class SettingsMethods:
    """Settings-related JSON-RPC handlers, mixed into GatewaySession."""

    async def _settings(self):
        if self._settings_factory is None:
            raise RuntimeError("settings factory was not configured by bootstrap")
        return await self._settings_factory(self._workspace or ".")

    async def _method_settings_get(self, params: dict) -> dict:
        settings = await self._settings()
        return await self._desktop_settings_snapshot(settings)

    async def _method_settings_update(self, params: dict) -> dict:
        from voidx.config.enums import PermissionMode
        from voidx.platform.code_ide import CodeIde
        from voidx.config.models import AiApprovalConfig, CompactionConfig, Profile

        patch = params.get("patch", {})
        if not isinstance(patch, dict):
            raise MethodParamsError("patch is required")

        settings = await self._settings()

        permissions = patch.get("permissions")
        if permissions is not None:
            if not isinstance(permissions, dict):
                raise MethodParamsError("invalid permissions")
            preset = permissions.get("permission_mode", settings.get_permission_mode().value)
            try:
                settings.set_permission_mode(PermissionMode(str(preset)))
            except ValueError as exc:
                raise MethodParamsError("invalid permission_mode") from exc
            if "ai_approval" in permissions:
                raw_ai = permissions["ai_approval"]
                if not isinstance(raw_ai, dict):
                    raise MethodParamsError("invalid ai_approval")
                current_ai = settings.get_ai_approval_config()
                profile_name = raw_ai.get("profile_name", current_ai.profile_name)
                timeout_seconds = raw_ai.get("timeout_seconds", current_ai.timeout_seconds)
                try:
                    ai_config = AiApprovalConfig(profile_name=str(profile_name), timeout_seconds=timeout_seconds)
                except Exception as exc:
                    raise MethodParamsError("invalid ai_approval") from exc
                if ai_config.profile_name:
                    profiles = await settings.list_profiles()
                    profile = next((item for item in profiles if item.name == ai_config.profile_name), None)
                    if profile is None or not profile.api_key:
                        raise MethodParamsError("invalid ai_approval profile")
                settings.set_ai_approval_config(ai_config)

            for key, setter in (
                ("sandbox_readable_files", settings.set_sandbox_readable_files),
                ("sandbox_readable_dirs", settings.set_sandbox_readable_dirs),
                ("sandbox_writable_files", settings.set_sandbox_writable_files),
                ("sandbox_writable_dirs", settings.set_sandbox_writable_dirs),
            ):
                if key in permissions:
                    paths = permissions[key]
                    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                        raise MethodParamsError(f"invalid {key}")
                    setter(paths)

        user_profile = patch.get("user_profile")
        if user_profile is not None:
            if not isinstance(user_profile, dict):
                raise MethodParamsError("invalid user_profile")
            if "language" in user_profile:
                settings.set_user_language(str(user_profile["language"] or ""))
            if "tone" in user_profile:
                settings.set_user_tone(str(user_profile["tone"] or ""))


        update_check = patch.get("update_check")
        if update_check is not None:
            if not isinstance(update_check, dict):
                raise MethodParamsError("invalid update_check")
            if "enabled" in update_check:
                settings.set_update_check_enabled(bool(update_check["enabled"]))

        if "code_ide" in patch:
            try:
                settings.set_code_ide(CodeIde(patch["code_ide"]))
            except ValueError as exc:
                raise MethodParamsError("invalid code_ide") from exc

        compaction_patch = patch.get("compaction")
        if compaction_patch is not None:
            if not isinstance(compaction_patch, dict):
                raise MethodParamsError("invalid compaction")
            current_compaction = settings.get_compaction_config()
            try:
                compaction_config = CompactionConfig(
                    profile_name=str(compaction_patch.get("profile_name", current_compaction.profile_name) or ""),
                    reasoning_effort=compaction_patch.get("reasoning_effort", current_compaction.reasoning_effort),
                    timeout_seconds=compaction_patch.get("timeout_seconds", current_compaction.timeout_seconds),
                )
            except Exception as exc:
                raise MethodParamsError("invalid compaction") from exc
            if compaction_config.profile_name:
                profiles = await settings.list_profiles()
                profile = next((item for item in profiles if item.name == compaction_config.profile_name), None)
                if profile is None or not profile.api_key:
                    raise MethodParamsError("invalid compaction profile")
            settings.set_compaction_config(compaction_config)

        # model reconfiguration
        model_patch = patch.get("model")
        if model_patch is not None:
            if not isinstance(model_patch, dict):
                raise MethodParamsError("invalid model")
            if any(k in model_patch for k in ("provider", "model", "base_url", "protocol")):
                current_profile = await settings.resolve_profile()
                default_provider = current_profile.provider if current_profile else "anthropic"
                default_model = (
                    current_profile.model
                    if current_profile
                    else settings.default_model
                )
                provider = model_patch.get("provider") or default_provider
                model_name = model_patch.get("model") or default_model
                profile_name = f"{provider}/{model_name}"
                try:
                    existing_profiles = await settings.list_profiles()
                    existing_profile = next(
                        (profile for profile in existing_profiles if profile.name == profile_name),
                        None,
                    )
                    await settings.save_profile(Profile(
                        name=profile_name,
                        api_key=existing_profile.api_key if existing_profile else "",
                        base_url=(
                            model_patch["base_url"]
                            if "base_url" in model_patch
                            else existing_profile.base_url if existing_profile else None
                        ),
                        protocol=(
                            model_patch["protocol"]
                            if "protocol" in model_patch
                            else existing_profile.protocol if existing_profile else None
                        ),
                    ), scope="local")
                except Exception as exc:
                    raise MethodParamsError(f"model save failed: {exc}") from exc

        # reasoning / context
        if "reasoning_effort" in model_patch if model_patch else {}:
            effort = str(model_patch["reasoning_effort"] or "")
            try:
                settings.set_reasoning_effort(effort or None)
            except ValueError as exc:
                raise MethodParamsError(f"invalid reasoning_effort: {effort}") from exc

        if "context_window" in (model_patch or {}):
            ctx = model_patch["context_window"]
            if ctx is not None and (not isinstance(ctx, int) or ctx < 1):
                raise MethodParamsError("invalid context_window")
            if ctx is None:
                settings._pop_setting("context_window")
            else:
                settings._set_setting("context_window", ctx)

        # provider secrets
        secrets_patch = patch.get("provider_secrets")
        if secrets_patch is not None:
            if not isinstance(secrets_patch, dict):
                raise MethodParamsError("invalid provider_secrets")
            provider = secrets_patch.get("provider", "")
            action = secrets_patch.get("action", "set")
            if not provider:
                raise MethodParamsError("provider is required")
            if action not in ("set", "delete"):
                raise MethodParamsError("invalid action")
            if action == "set":
                api_key = secrets_patch.get("api_key", "")
                if not isinstance(api_key, str) or not api_key.strip():
                    raise MethodParamsError("api_key is required")
                # build profile name from provider + first known model
                profile_name = secrets_patch.get("profile_name")
                if not profile_name:
                    # find existing profile for this provider
                    existing = await settings.list_profiles()
                    match = next((p for p in existing if p.provider == provider), None)
                    profile_name = match.name if match else f"{provider}/default"
                existing_profiles = await settings.list_profiles()
                existing_profile = next(
                    (profile for profile in existing_profiles if profile.name == profile_name),
                    None,
                )
                await settings.save_profile(Profile(
                    name=profile_name,
                    api_key=api_key.strip(),
                    base_url=existing_profile.base_url if existing_profile else None,
                    protocol=existing_profile.protocol if existing_profile else None,
                ), scope="local")
            else:
                profile_name = secrets_patch.get("profile_name")
                if not profile_name:
                    existing = await settings.list_profiles()
                    match = next((p for p in existing if p.provider == provider), None)
                    if match is None:
                        raise MethodParamsError("no profile found for provider")
                    profile_name = match.name
                await settings.delete_profile(profile_name)

        handler = getattr(self, "_settings_update_handler", None)
        if callable(handler):
            import inspect

            result = handler(settings)
            if inspect.isawaitable(result):
                await result

        return {"ok": True, "settings": await self._desktop_settings_snapshot(settings)}

    async def _desktop_settings_snapshot(self, settings) -> dict:
        profile = await settings.resolve_profile()
        profiles = await settings.list_profiles()
        model = {
            "provider": profile.provider if profile else "anthropic",
            "model": (
                profile.model
                if profile
                else settings.default_model
            ),
            "base_url": profile.base_url if profile else None,
            "protocol": profile.protocol if profile else None,
            "reasoning_effort": settings._effective_data().get("reasoning_effort") or "xhigh",
            "context_window": settings._effective_data().get("context_window"),
        }
        return {
            "model": model,
            "profiles": [
                {
                    "name": profile_item.name,
                    "provider": profile_item.provider,
                    "model": profile_item.model,
                    "base_url": profile_item.base_url,
                    "protocol": profile_item.protocol,
                    "configured": bool(profile_item.api_key),
                }
                for profile_item in profiles
            ],
            "compaction": settings.get_compaction_config().model_dump(mode="json"),
            "permissions": {
                "permission_mode": settings.get_permission_mode().value,
                "ai_approval": settings.get_ai_approval_config().model_dump(mode="json"),
                "sandbox_readable_files": settings.get_sandbox_readable_files(),
                "sandbox_readable_dirs": settings.get_sandbox_readable_dirs(),
                "sandbox_writable_files": settings.get_sandbox_writable_files(),
                "sandbox_writable_dirs": settings.get_sandbox_writable_dirs(),
            },
            "user_profile": settings.get_user_profile().model_dump(),
            "code_ide": settings.get_code_ide().value,
            "update_check": {
                "enabled": settings.get_update_check_enabled(),
                "last_checked_at": settings.get_update_check_last_checked_at(),
                "latest_version": settings.get_update_check_latest_version(),
            },
            "paths": {
                "workspace_settings": str(settings.path),
                "global_settings": str(settings._global_path),
                "skills_state": str(settings.skills_path),
            },
        }

    async def _method_integrations_get(self, params: dict) -> dict:
        settings = self._gateway_settings()
        return {
            "mcp_servers": [self._mcp_server_summary(server) for server in settings.list_mcp_servers()],
            "web_routes": {
                "search": settings.get_web_tool_route("search").model_dump(),
                "fetch": settings.get_web_tool_route("fetch").model_dump(),
            },
            "tavily": self._tavily_summary(settings),
            "bocha": self._bocha_summary(settings),
            "skills": self._skill_summaries(settings),
            "lsp": await self._lsp_status_list(),
            "warnings": [],
        }

    def _gateway_settings(self):
        from voidx.config.settings import Settings
        return Settings(self._workspace or ".")

    @staticmethod
    def _settings_for_scope(scope: str, workspace: str):
        from voidx.config.settings import Settings
        return Settings(workspace)
