"""JSON-backed workspace settings."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from voidx.config.enums import PermissionMode
from voidx.config.models import Config, ModelConfig, Profile, UserProfile
from voidx.config.permissions import permission_mode_defaults, permission_mode_reviewer_default
from voidx.config.settings_agent import SettingsAgentMixin
from voidx.config.settings_api_keys import SettingsApiKeyMixin
from voidx.config.settings_code_ide import SettingsCodeIdeMixin
from voidx.config.settings_custom import SettingsCustomProviderMixin
from voidx.config.settings_mcp import SettingsMcpMixin
from voidx.config.settings_permissions import SettingsPermissionMixin
from voidx.config.settings_skills import SettingsSkillsMixin
from voidx.config.settings_update import SettingsUpdateMixin
from voidx.config.settings_web import SettingsWebMixin

SETTINGS_FILE = ".voidx/settings.json"
SKILLS_STATE_FILE = ".voidx/skills.json"
_LEGACY_SETTINGS_FILE = "voidx.json"
_PROFILE_UNSET = object()
GLOBAL_KEYS = frozenset({
    "current_profile",
    "mcpServers",
    "tavily_api_key",
    "codeIde",
    "userProfile",
    "web",
    "update_check",
    "parallel_subagents",
})
WORKSPACE_ONLY_KEYS = frozenset({
    "permission_mode",
    "sandbox_mode",
    "sandbox_workspace_write",
    "approval_policy",
    "approval_reviewer",
    "ask_compact",
    "skills",
})


def _settings_home() -> Path:
    return Path.home()


class Settings(
    SettingsAgentMixin,
    SettingsApiKeyMixin,
    SettingsMcpMixin,
    SettingsWebMixin,
    SettingsSkillsMixin,
    SettingsUpdateMixin,
    SettingsPermissionMixin,
    SettingsCodeIdeMixin,
    SettingsCustomProviderMixin,
):
    """Persistent settings backed by ``.voidx/settings.json`` in the workspace directory."""

    def __init__(self, workspace: str = ".") -> None:
        self._workspace = Path(workspace).resolve()
        self._path = self._workspace / SETTINGS_FILE
        self._global_path = _settings_home() / SETTINGS_FILE
        self._migrate_legacy_file()
        self._data: dict = self._load()
        self._global_data: dict = {} if self._global_path == self._path else self._load_path(self._global_path)
        self._runtime_keys: dict[str, str] = {}
        self._effective_cache: dict | None = None

    @classmethod
    async def create(cls, workspace: str = ".") -> Settings:
        settings = cls.__new__(cls)
        settings._workspace = Path(workspace).resolve()
        settings._path = settings._workspace / SETTINGS_FILE
        settings._global_path = _settings_home() / SETTINGS_FILE
        settings._migrate_legacy_file()
        settings._data = settings._load()
        settings._global_data = {} if settings._global_path == settings._path else settings._load_path(settings._global_path)
        settings._runtime_keys = {}
        settings._effective_cache = None
        await settings._migrate_legacy_profiles()
        await settings._migrate_to_global()
        return settings

    def _migrate_legacy_file(self) -> None:
        legacy = self._workspace / _LEGACY_SETTINGS_FILE
        if legacy.exists() and not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(self._path)

    def _load(self) -> dict:
        return self._load_path(self._path)

    def _load_path(self, path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self) -> None:
        self._effective_cache = None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _save_global(self) -> None:
        self._effective_cache = None
        self._global_path.parent.mkdir(parents=True, exist_ok=True)
        self._global_path.write_text(
            json.dumps(self._global_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _effective_data(self) -> dict:
        if self._effective_cache is not None:
            return deepcopy(self._effective_cache)
        merged: dict = {}
        for key, value in self._global_data.items():
            if key in GLOBAL_KEYS:
                merged[key] = deepcopy(value)
        for key, value in self._data.items():
            if key not in GLOBAL_KEYS:
                continue
            if key in merged and isinstance(value, dict) and isinstance(merged[key], dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = deepcopy(value)
        for key, value in self._data.items():
            if key not in GLOBAL_KEYS:
                merged[key] = deepcopy(value)
        self._effective_cache = deepcopy(merged)
        return merged

    def _write_target(self, key: str) -> tuple[dict, Path, str]:
        if self._global_path == self._path:
            return self._data, self._path, "workspace"
        if key in WORKSPACE_ONLY_KEYS:
            return self._data, self._path, "workspace"
        if key in GLOBAL_KEYS and key not in self._data:
            return self._global_data, self._global_path, "global"
        return self._data, self._path, "workspace"

    def _save_target(self, target: str) -> None:
        if target == "global":
            self._save_global()
        else:
            self._save()

    def _set_setting(self, key: str, value) -> Path:
        data, path, target = self._write_target(key)
        data[key] = value
        self._save_target(target)
        return path

    def _pop_setting(self, key: str) -> Path:
        data, path, target = self._write_target(key)
        data.pop(key, None)
        self._save_target(target)
        return path

    def _target_mapping(self, key: str) -> tuple[dict, Path, str]:
        data, path, target = self._write_target(key)
        value = data.get(key)
        if isinstance(value, dict):
            return dict(value), path, target
        return {}, path, target

    def _save_target_mapping(self, key: str, value: dict, target: str) -> Path:
        if target == "global":
            self._global_data[key] = value
            self._save_global()
            return self._global_path
        self._data[key] = value
        self._save()
        return self._path

    @property
    def path(self) -> Path:
        return self._path

    @property
    def skills_path(self) -> Path:
        return self._workspace / SKILLS_STATE_FILE

    # ── profiles API ─────────────────────────────────────────────────────

    async def list_profiles(self) -> list[Profile]:
        from voidx.memory.service import list_model_profiles_async

        return [
            Profile(
                name=row.name,
                api_key=row.api_key,
                base_url=row.base_url,
                protocol=row.protocol,
            )
            for row in await list_model_profiles_async()
        ]

    async def resolve_profile(self, name: str = "") -> Profile | None:
        if not name:
            name = self._effective_data().get("current_profile", "")
        if name:
            profile = await self._get_profile(name)
            if profile is not None:
                return profile
        profiles = await self.list_profiles()
        return profiles[0] if profiles else None

    async def save_profile(self, profile: Profile) -> Path:
        from voidx.memory.service import ModelProfileRow, save_model_profile_async

        await save_model_profile_async(ModelProfileRow(
            name=profile.name,
            provider=profile.provider,
            model=profile.model,
            api_key=profile.api_key,
            base_url=profile.base_url,
            protocol=profile.protocol,
        ))
        return self._set_setting("current_profile", profile.name)

    async def delete_profile(self, name: str) -> Path:
        from voidx.memory.service import delete_model_profile_async

        await delete_model_profile_async(name)
        if self._effective_data().get("current_profile") == name:
            profiles = await self.list_profiles()
            next_profile = profiles[0] if profiles else None
            if next_profile is not None:
                return self._set_setting("current_profile", next_profile.name)
            else:
                return self._pop_setting("current_profile")
        return self._path

    # ── cross-profile lookups ────────────────────────────────────────────

    # ── MCP servers ─────────────────────────────────────────────────────

    # ── web tool routing ─────────────────────────────────────────────────

    # ── skills ──────────────────────────────────────────────────────────

    # ── sandbox / approval ───────────────────────────────────────────────

    # ── code IDE ─────────────────────────────────────────────────────────

    # ── custom models ─────────────────────────────────────────────────────

    # ── user profile ──────────────────────────────────────────────────────

    def get_user_profile(self) -> UserProfile:
        data = self._effective_data()
        raw = data.get("userProfile")
        if not isinstance(raw, dict):
            raw = data.get("user_profile")
        if not isinstance(raw, dict):
            raw = {}
        language = raw.get("language", data.get("user_language", ""))
        tone = raw.get("tone", data.get("user_tone", ""))
        return UserProfile(
            language=_normalize_user_language(language),
            tone=_normalize_user_tone(tone),
        )

    def set_user_language(self, language: str) -> Path:
        profile = self.get_user_profile()
        profile.language = _normalize_user_language(language)
        return self._save_user_profile(profile)

    def set_user_tone(self, tone: str) -> Path:
        profile = self.get_user_profile()
        profile.tone = _normalize_user_tone(tone)
        return self._save_user_profile(profile)

    def _save_user_profile(self, profile: UserProfile) -> Path:
        payload: dict[str, str] = {}
        if profile.language:
            payload["language"] = profile.language
        if profile.tone:
            payload["tone"] = profile.tone
        if payload:
            path = self._set_setting("userProfile", payload)
        else:
            path = self._pop_setting("userProfile")
        changed = False
        for key in ("user_profile", "user_language", "user_tone"):
            if self._data.pop(key, None) is not None:
                changed = True
        if changed:
            self._save()
        return path

    # ── build config for graph ───────────────────────────────────────────

    async def build_config(self, profile: Profile | None | object = _PROFILE_UNSET) -> Config:
        if profile is _PROFILE_UNSET:
            profile = await self.resolve_profile()
        if profile:
            provider = profile.provider
            model = profile.model
            base_url = profile.base_url
            protocol = profile.protocol
        else:
            provider = "anthropic"
            model = "claude-sonnet-4-6"
            base_url = None
            protocol = None

        cfg = ModelConfig(provider=provider, model=model, base_url=base_url)
        if protocol:
            cfg.protocol = protocol
        permission_mode = self.get_permission_mode()
        if permission_mode == PermissionMode.CUSTOM:
            sandbox_mode = self.get_sandbox_mode()
            approval_policy = self.get_approval_policy()
            approval_reviewer = self.get_approval_reviewer()
        else:
            sandbox_mode, approval_policy = permission_mode_defaults(permission_mode)
            approval_reviewer = permission_mode_reviewer_default(permission_mode)

        return Config(
            model=cfg,
            parallel_subagents=self.get_parallel_subagents(),
            permission_mode=permission_mode,
            sandbox_mode=sandbox_mode,
            sandbox_workspace_write=self.get_sandbox_workspace_write(),
            approval_policy=approval_policy,
            approval_reviewer=approval_reviewer,
            ask_compact=bool(self._effective_data().get(
                "askCompact",
                self._effective_data().get("ask_compact", False),
            )),
            user_profile=self.get_user_profile(),
        )

    async def _get_profile(self, name: str) -> Profile | None:
        from voidx.memory.service import get_model_profile_async

        row = await get_model_profile_async(name)
        if row is None:
            return None
        return Profile(
            name=row.name,
            api_key=row.api_key,
            base_url=row.base_url,
            protocol=row.protocol,
        )

    async def _migrate_legacy_profiles(self) -> None:
        from voidx.memory.service import ModelProfileRow, save_model_profile_async

        profiles_data = self._data.get("profiles", {})
        if not isinstance(profiles_data, dict):
            profiles_data = {}

        custom_providers = self._data.get("custom_providers", {})
        if not isinstance(custom_providers, dict):
            custom_providers = {}

        changed = False
        first_imported: str | None = None
        for name, fields in profiles_data.items():
            if not isinstance(fields, dict) or not fields.get("api_key"):
                continue
            provider = name.split("/", 1)[0] if "/" in name else name
            provider_fields = custom_providers.get(provider, {})
            if not isinstance(provider_fields, dict):
                provider_fields = {}
            base_url = fields.get("base_url") or provider_fields.get("base_url") or None
            protocol = fields.get("protocol") or provider_fields.get("protocol") or None
            profile = Profile(
                name=name,
                api_key=fields["api_key"],
                base_url=base_url,
                protocol=protocol,
            )
            await save_model_profile_async(ModelProfileRow(
                name=profile.name,
                provider=profile.provider,
                model=profile.model,
                api_key=profile.api_key,
                base_url=profile.base_url,
                protocol=profile.protocol,
            ))
            first_imported = first_imported or profile.name
            changed = True

        legacy_current = self._data.get("default_profile")
        if legacy_current:
            self._data["current_profile"] = legacy_current
            changed = True
        elif first_imported and not self._data.get("current_profile"):
            self._data["current_profile"] = first_imported
            changed = True
        if "profiles" in self._data:
            self._data.pop("profiles", None)
            changed = True
        if "default_profile" in self._data:
            self._data.pop("default_profile", None)
            changed = True
        if "custom_models" in self._data:
            self._data.pop("custom_models", None)
            changed = True
        if "custom_providers" in self._data:
            self._data.pop("custom_providers", None)
            changed = True
        if changed:
            self._save()

    async def _migrate_to_global(self) -> None:
        if self._global_path == self._path or self._global_path.exists():
            return
        global_items = {
            key: deepcopy(self._data[key])
            for key in GLOBAL_KEYS
            if key in self._data
        }
        if not global_items:
            return
        self._global_data = global_items
        self._save_global()
        for key in global_items:
            self._data.pop(key, None)
        self._save()


def _normalize_user_language(value: object) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "auto", "detect", "default"}:
        return ""
    return text


def _normalize_user_tone(value: object) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "auto", "default"}:
        return ""
    return text
