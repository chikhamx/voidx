"""JSON-backed workspace settings."""

from __future__ import annotations

import json
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
from voidx.config.settings_web import SettingsWebMixin

SETTINGS_FILE = ".voidx/settings.json"
SKILLS_STATE_FILE = ".voidx/skills.json"
_LEGACY_SETTINGS_FILE = "voidx.json"


class Settings(
    SettingsAgentMixin,
    SettingsApiKeyMixin,
    SettingsMcpMixin,
    SettingsWebMixin,
    SettingsSkillsMixin,
    SettingsPermissionMixin,
    SettingsCodeIdeMixin,
    SettingsCustomProviderMixin,
):
    """Persistent settings backed by ``.voidx/settings.json`` in the workspace directory."""

    def __init__(self, workspace: str = ".") -> None:
        self._workspace = Path(workspace).resolve()
        self._path = self._workspace / SETTINGS_FILE
        self._migrate_legacy_file()
        self._data: dict = self._load()
        self._runtime_keys: dict[str, str] = {}

    @classmethod
    async def create(cls, workspace: str = ".") -> Settings:
        settings = cls.__new__(cls)
        settings._workspace = Path(workspace).resolve()
        settings._path = settings._workspace / SETTINGS_FILE
        settings._migrate_legacy_file()
        settings._data = settings._load()
        settings._runtime_keys = {}
        await settings._migrate_legacy_profiles()
        return settings

    def _migrate_legacy_file(self) -> None:
        legacy = self._workspace / _LEGACY_SETTINGS_FILE
        if legacy.exists() and not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(self._path)

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def skills_path(self) -> Path:
        return self._workspace / SKILLS_STATE_FILE

    # ── profiles API ─────────────────────────────────────────────────────

    async def list_profiles(self) -> list[Profile]:
        from voidx.memory.model_profiles import list_model_profiles_async

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
            name = self._data.get("current_profile", "")
        if name:
            profile = await self._get_profile(name)
            if profile is not None:
                return profile
        profiles = await self.list_profiles()
        return profiles[0] if profiles else None

    async def save_profile(self, profile: Profile) -> Path:
        from voidx.memory.model_profiles import ModelProfileRow, save_model_profile_async

        await save_model_profile_async(ModelProfileRow(
            name=profile.name,
            provider=profile.provider,
            model=profile.model,
            api_key=profile.api_key,
            base_url=profile.base_url,
            protocol=profile.protocol,
        ))
        self._data["current_profile"] = profile.name
        self._save()
        return self._path

    async def delete_profile(self, name: str) -> Path:
        from voidx.memory.model_profiles import delete_model_profile_async

        await delete_model_profile_async(name)
        if self._data.get("current_profile") == name:
            profiles = await self.list_profiles()
            next_profile = profiles[0] if profiles else None
            if next_profile is not None:
                self._data["current_profile"] = next_profile.name
            else:
                self._data.pop("current_profile", None)
        self._save()
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
        raw = self._data.get("userProfile")
        if not isinstance(raw, dict):
            raw = self._data.get("user_profile")
        if not isinstance(raw, dict):
            raw = {}
        language = raw.get("language", self._data.get("user_language", ""))
        tone = raw.get("tone", self._data.get("user_tone", ""))
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
            self._data["userProfile"] = payload
        else:
            self._data.pop("userProfile", None)
        self._data.pop("user_profile", None)
        self._data.pop("user_language", None)
        self._data.pop("user_tone", None)
        self._save()
        return self._path

    # ── build config for graph ───────────────────────────────────────────

    async def build_config(self) -> Config:
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

        # Check if provider is a custom provider
        if not base_url:
            for cp in self.list_custom_providers():
                if cp["name"] == provider:
                    protocol = protocol or cp["protocol"]
                    if cp["base_url"]:
                        base_url = cp["base_url"]
                    break

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
            agent_max_steps=self.get_agent_max_steps(),
            permission_mode=permission_mode,
            sandbox_mode=sandbox_mode,
            sandbox_workspace_write=self.get_sandbox_workspace_write(),
            approval_policy=approval_policy,
            approval_reviewer=approval_reviewer,
            ask_compact=bool(self._data.get("askCompact", self._data.get("ask_compact", False))),
            user_profile=self.get_user_profile(),
        )

    async def _get_profile(self, name: str) -> Profile | None:
        from voidx.memory.model_profiles import get_model_profile_async

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
        from voidx.memory.model_profiles import ModelProfileRow, save_model_profile_async

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
