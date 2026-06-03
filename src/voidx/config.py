"""Configuration system — typed, JSON-backed, no .env restrictions."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

SETTINGS_FILE = ".voidx/settings.json"
SKILLS_STATE_FILE = ".voidx/skills.json"
_LEGACY_SETTINGS_FILE = "voidx.json"


class SandboxMode(str, Enum):
    """Filesystem boundary control — mirrors Codex CLI sandbox modes.

    read-only:        All write/edit/bash/lsp_format tools are denied.
    workspace-write:  Only writes inside the workspace (+ extra_paths) are allowed.
    danger-full-access: No filesystem restrictions (current voidx behaviour).
    """
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ApprovalPolicy(str, Enum):
    """How often voidx asks for human confirmation on tool calls.

    untrusted:   Write/edit/write-capable bash/implement agent tools ask.
    on-failure:  Auto-allow non-bash ask tools, then report failures.
    on-request:  Auto-allow; only ask when the agent explicitly requests approval.
    never:       Full auto — no human-in-the-loop (equivalent to --full-auto).
    """
    UNTRUSTED = "untrusted"
    ON_FAILURE = "on-failure"
    ON_REQUEST = "on-request"
    NEVER = "never"


class ApprovalReviewer(str, Enum):
    """Who handles approval prompts when a tool call needs a decision."""
    USER = "user"
    AUTO_REVIEW = "auto_review"


class CodeIde(str, Enum):
    """Preferred app for opening files from the review panel."""
    AUTO = "auto"
    TRAE = "trae"
    CURSOR = "cursor"
    CODE = "code"
    WINDSURF = "windsurf"
    ZED = "zed"
    SUBLIME = "sublime"
    JETBRAINS = "jetbrains"
    GHOSTTY = "ghostty"
    SYSTEM = "system"


class PermissionMode(str, Enum):
    """User-facing presets for sandbox + approval behavior."""
    DEFAULT = "default"
    READ_ONLY = "read-only"
    ACCEPT_EDITS = "accept-edits"
    AUTO_REVIEW = "auto-review"
    FULL_ACCESS = "full-access"
    CUSTOM = "custom"


def permission_mode_defaults(mode: PermissionMode) -> tuple[SandboxMode, ApprovalPolicy]:
    if mode == PermissionMode.DEFAULT:
        return SandboxMode.WORKSPACE_WRITE, ApprovalPolicy.UNTRUSTED
    if mode == PermissionMode.READ_ONLY:
        return SandboxMode.READ_ONLY, ApprovalPolicy.UNTRUSTED
    if mode == PermissionMode.ACCEPT_EDITS:
        return SandboxMode.WORKSPACE_WRITE, ApprovalPolicy.UNTRUSTED
    if mode == PermissionMode.AUTO_REVIEW:
        return SandboxMode.WORKSPACE_WRITE, ApprovalPolicy.UNTRUSTED
    if mode == PermissionMode.FULL_ACCESS:
        return SandboxMode.DANGER_FULL_ACCESS, ApprovalPolicy.NEVER
    return SandboxMode.WORKSPACE_WRITE, ApprovalPolicy.UNTRUSTED


def permission_mode_reviewer_default(mode: PermissionMode) -> ApprovalReviewer:
    if mode == PermissionMode.AUTO_REVIEW:
        return ApprovalReviewer.AUTO_REVIEW
    return ApprovalReviewer.USER


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


class Profile(BaseModel):
    """A named LLM configuration.  Name is ``provider/model`` (e.g. ``mimo/mimo-v2.5-pro``)."""
    name: str
    api_key: str = ""
    base_url: str | None = None
    protocol: str | None = None
    @property
    def provider(self) -> str:
        return self.name.split("/", 1)[0] if "/" in self.name else self.name

    @property
    def model(self) -> str:
        return self.name.split("/", 1)[1] if "/" in self.name else self.name


class ModelConfig(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    base_url: str | None = None
    protocol: str | None = None  # "openai" | "anthropic" | "gemini" | None (auto-detect)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=8192, ge=1, le=128000)
    reasoning_effort: str | None = Field(
        default="xhigh",
        description="Reasoning intensity: off, low, medium, high, xhigh, or None (provider default)",
    )


class AgentConfig(BaseModel):
    name: str
    description: str = ""
    model: ModelConfig | None = None
    max_steps: int = Field(default=50, ge=1, le=500)
    recursion_limit: int = Field(default=200, ge=1, le=1000)
    tools: set[str] | None = None


class McpServerConfig(BaseModel):
    name: str
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False
    tools: list[str] | dict[str, object] | None = None
    transport: str = "stdio"  # "stdio" | "sse" (future)

    @property
    def tool_count(self) -> int:
        if isinstance(self.tools, dict):
            return len(self.tools)
        if isinstance(self.tools, list):
            return len(self.tools)
        return 0


class WebToolRoute(BaseModel):
    backend: str = "legacy"  # "legacy" | "mcp"
    server: str = ""
    tool: str = ""


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=lambda: AgentConfig(
        name="build", description="Primary coding agent.",
    ))
    workspace: str = "."
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    sandbox_mode: SandboxMode = SandboxMode.WORKSPACE_WRITE
    sandbox_workspace_write: list[str] = Field(
        default_factory=list,
        description="Extra paths writable under workspace-write sandbox mode.",
    )
    approval_policy: ApprovalPolicy = ApprovalPolicy.UNTRUSTED
    approval_reviewer: ApprovalReviewer = ApprovalReviewer.USER
    ask_compact: bool = False


# ── JSON-backed settings store ────────────────────────────────────────────

class Settings:
    """Persistent settings backed by ``.voidx/settings.json`` in the workspace directory."""

    def __init__(self, workspace: str = ".") -> None:
        self._workspace = Path(workspace).resolve()
        self._path = self._workspace / SETTINGS_FILE
        self._migrate_legacy_file()
        self._data: dict = self._load()
        self._runtime_keys: dict[str, str] = {}
        self._migrate_legacy_profiles()

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

    def list_profiles(self) -> list[Profile]:
        from voidx.memory.model_profiles import list_model_profiles

        return [
            Profile(
                name=row.name,
                api_key=row.api_key,
                base_url=row.base_url,
                protocol=row.protocol,
            )
            for row in list_model_profiles()
        ]

    def resolve_profile(self, name: str = "") -> Profile | None:
        if not name:
            name = self._data.get("current_profile", "")
        if name:
            profile = self._get_profile(name)
            if profile is not None:
                return profile
        profiles = self.list_profiles()
        return profiles[0] if profiles else None

    def save_profile(self, profile: Profile) -> Path:
        from voidx.memory.model_profiles import ModelProfileRow, save_model_profile

        save_model_profile(ModelProfileRow(
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

    def delete_profile(self, name: str) -> Path:
        from voidx.memory.model_profiles import delete_model_profile

        delete_model_profile(name)
        if self._data.get("current_profile") == name:
            next_profile = self.list_profiles()[0] if self.list_profiles() else None
            if next_profile is not None:
                self._data["current_profile"] = next_profile.name
            else:
                self._data.pop("current_profile", None)
        self._save()
        return self._path

    # ── cross-profile lookups ────────────────────────────────────────────

    def resolve_api_key(self, provider: str) -> str | None:
        runtime = self._runtime_keys.get(provider)
        if runtime:
            return runtime
        for p in self.list_profiles():
            if p.provider == provider:
                return p.api_key
        return None

    def set_runtime_api_key(self, provider: str, key: str) -> None:
        self._runtime_keys[provider] = key

    def resolve_base_url(self, provider: str) -> str | None:
        for p in self.list_profiles():
            if p.provider == provider and p.base_url:
                return p.base_url
        for cp in self.list_custom_providers():
            if cp["name"] == provider and cp["base_url"]:
                return cp["base_url"]
        return None

    def resolve_protocol(self, provider: str) -> str | None:
        for p in self.list_profiles():
            if p.provider == provider and p.protocol:
                return p.protocol
        for cp in self.list_custom_providers():
            if cp["name"] == provider:
                return cp["protocol"]
        return None

    # ── tavily API key ─────────────────────────────────────────────────────

    def get_tavily_api_key(self) -> str | None:
        """Get Tavily API key. Env var TAVILY_API_KEY takes priority over config file."""
        import os
        env_key = os.environ.get("TAVILY_API_KEY")
        if env_key:
            return env_key
        return self._data.get("tavily_api_key") or None

    def set_tavily_api_key(self, api_key: str) -> None:
        self._data["tavily_api_key"] = api_key
        self._save()

    def delete_tavily_api_key(self) -> None:
        self._data.pop("tavily_api_key", None)
        self._save()

    # ── MCP servers ─────────────────────────────────────────────────────

    def list_mcp_servers(self) -> list[McpServerConfig]:
        servers_data = self._data.get("mcpServers") or self._data.get("mcp_servers") or {}
        if not isinstance(servers_data, dict):
            return []

        result: list[McpServerConfig] = []
        for name, fields in servers_data.items():
            if not isinstance(fields, dict):
                continue
            try:
                result.append(McpServerConfig(name=name, **fields))
            except ValueError:
                continue
        return result

    def get_mcp_server(self, name: str) -> McpServerConfig | None:
        for server in self.list_mcp_servers():
            if server.name == name:
                return server
        return None

    def save_mcp_server(self, server: McpServerConfig) -> Path:
        servers = self._mcp_servers_data()
        servers[server.name] = server.model_dump(exclude={"name"}, exclude_none=True)
        self._data["mcpServers"] = servers
        self._save()
        return self._path

    def delete_mcp_server(self, name: str) -> Path:
        servers = self._mcp_servers_data()
        servers.pop(name, None)
        self._data["mcpServers"] = servers
        self.clear_web_routes_for_server(name)
        self._save()
        return self._path

    def _mcp_servers_data(self) -> dict:
        servers = self._data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = self._data.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = {}
        return dict(servers)

    # ── web tool routing ─────────────────────────────────────────────────

    def get_web_tool_route(self, kind: str) -> WebToolRoute:
        web = self._data.get("web", {})
        if not isinstance(web, dict):
            return WebToolRoute()
        fields = web.get(kind, {})
        if not isinstance(fields, dict):
            return WebToolRoute()
        try:
            return WebToolRoute(**fields)
        except ValueError:
            return WebToolRoute()

    def set_web_tool_route(self, kind: str, route: WebToolRoute) -> Path:
        web = self._data.get("web", {})
        if not isinstance(web, dict):
            web = {}
        web[kind] = route.model_dump()
        self._data["web"] = web
        self._save()
        return self._path

    def clear_web_routes_for_server(self, server: str) -> None:
        web = self._data.get("web", {})
        if not isinstance(web, dict):
            return
        for kind, fields in list(web.items()):
            if isinstance(fields, dict) and fields.get("server") == server:
                web[kind] = WebToolRoute().model_dump()

    # ── skills ──────────────────────────────────────────────────────────

    def get_skill_selection(self):
        from voidx.skills.schema import SkillSelectionConfig

        data = self._skills_data()
        return SkillSelectionConfig(
            enabled=set(_string_list(data.get("enabled", []))),
            disabled=set(_string_list(data.get("disabled", []))),
        )

    def set_skill_enabled(self, name: str, enabled: bool) -> Path:
        skills = self._skills_data()
        enabled_list = _string_list(skills.get("enabled", []))
        disabled_list = _string_list(skills.get("disabled", []))
        if enabled:
            if name not in enabled_list:
                enabled_list.append(name)
            disabled_list = [item for item in disabled_list if item != name]
        else:
            if name not in disabled_list:
                disabled_list.append(name)
            enabled_list = [item for item in enabled_list if item != name]
        skills["version"] = 1
        skills["enabled"] = sorted(enabled_list)
        skills["disabled"] = sorted(disabled_list)
        self._save_skills_data(skills)
        self._data.pop("skills", None)
        return self.skills_path

    def _skills_data(self) -> dict:
        if self.skills_path.exists():
            try:
                skills = json.loads(self.skills_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
            return dict(skills) if isinstance(skills, dict) else {}
        skills = self._data.get("skills", {})
        return dict(skills) if isinstance(skills, dict) else {}

    def _save_skills_data(self, data: dict) -> None:
        self.skills_path.parent.mkdir(parents=True, exist_ok=True)
        self.skills_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── sandbox / approval ───────────────────────────────────────────────

    def get_permission_mode(self) -> PermissionMode:
        raw = self._data.get("permission_mode")
        if raw is not None:
            try:
                return PermissionMode(raw)
            except ValueError:
                return PermissionMode.CUSTOM
        if (
            "sandbox_mode" in self._data
            or "approval_policy" in self._data
            or self.get_sandbox_workspace_write()
        ):
            return PermissionMode.CUSTOM
        return PermissionMode.DEFAULT

    def set_permission_mode(self, mode: PermissionMode) -> Path:
        self._data["permission_mode"] = mode.value
        if mode != PermissionMode.CUSTOM:
            sandbox_mode, approval_policy = permission_mode_defaults(mode)
            self._data["sandbox_mode"] = sandbox_mode.value
            self._data["approval_policy"] = approval_policy.value
            self._data["approval_reviewer"] = permission_mode_reviewer_default(mode).value
            self._data.pop("sandbox_workspace_write", None)
        self._save()
        return self._path

    def get_sandbox_mode(self) -> SandboxMode:
        raw = self._data.get("sandbox_mode", "workspace-write")
        try:
            return SandboxMode(raw)
        except ValueError:
            return SandboxMode.WORKSPACE_WRITE

    def set_sandbox_mode(self, mode: SandboxMode) -> Path:
        self._data["permission_mode"] = PermissionMode.CUSTOM.value
        self._data["sandbox_mode"] = mode.value
        self._save()
        return self._path

    def get_sandbox_workspace_write(self) -> list[str]:
        paths = self._data.get("sandbox_workspace_write", [])
        return _string_list(paths)

    def set_sandbox_workspace_write(self, paths: list[str]) -> Path:
        self._data["permission_mode"] = PermissionMode.CUSTOM.value
        self._data["sandbox_workspace_write"] = list(paths)
        self._save()
        return self._path

    def get_approval_policy(self) -> ApprovalPolicy:
        raw = self._data.get("approval_policy", "untrusted")
        try:
            return ApprovalPolicy(raw)
        except ValueError:
            return ApprovalPolicy.UNTRUSTED

    def set_approval_policy(self, policy: ApprovalPolicy) -> Path:
        self._data["permission_mode"] = PermissionMode.CUSTOM.value
        self._data["approval_policy"] = policy.value
        self._save()
        return self._path

    def get_approval_reviewer(self) -> ApprovalReviewer:
        raw = self._data.get("approval_reviewer", "user")
        try:
            return ApprovalReviewer(raw)
        except ValueError:
            return ApprovalReviewer.USER

    def set_approval_reviewer(self, reviewer: ApprovalReviewer) -> Path:
        self._data["permission_mode"] = PermissionMode.CUSTOM.value
        self._data["approval_reviewer"] = reviewer.value
        self._save()
        return self._path

    # ── code IDE ─────────────────────────────────────────────────────────

    def get_code_ide(self) -> CodeIde:
        raw = self._data.get("codeIde", CodeIde.TRAE.value)
        try:
            return CodeIde(raw)
        except ValueError:
            return CodeIde.TRAE

    def set_code_ide(self, ide: CodeIde) -> Path:
        self._data["codeIde"] = ide.value
        self._save()
        return self._path

    # ── custom models ─────────────────────────────────────────────────────

    def list_custom_models(self, provider: str) -> list[str]:
        """Return user-added custom model names for a provider."""
        custom = self._data.get("custom_models", {})
        result: list[str] = []
        if not isinstance(custom, dict):
            models = []
        else:
            models = custom.get(provider, [])
        if isinstance(models, list):
            result.extend(str(model) for model in models)
        for profile in self.list_profiles():
            if profile.provider == provider and profile.model not in result:
                result.append(profile.model)
        return result

    def add_custom_model(self, provider: str, model: str) -> None:
        """Legacy no-op. Custom models are derived from saved DB profiles."""
        _ = (provider, model)

    def remove_custom_model(self, provider: str, model: str) -> None:
        """Remove a custom model name for a provider. Saves."""
        custom = self._data.get("custom_models", {})
        if not isinstance(custom, dict):
            return
        models = custom.get(provider, [])
        if not isinstance(models, list):
            return
        if model in models:
            models.remove(model)
            if not models:
                del custom[provider]
            self._save()

    # ── custom providers ──────────────────────────────────────────────────

    def list_custom_providers(self) -> list[dict[str, str]]:
        """Return list of {name, protocol, base_url} for custom providers."""
        providers = self._data.get("custom_providers", {})
        if not isinstance(providers, dict):
            return []
        result: list[dict[str, str]] = []
        for name, fields in providers.items():
            if isinstance(fields, dict):
                result.append({
                    "name": name,
                    "protocol": fields.get("protocol", "openai"),
                    "base_url": fields.get("base_url", ""),
                })
        return result

    def add_custom_provider(self, name: str, protocol: str = "openai", base_url: str = "") -> None:
        """Legacy no-op. Provider protocol/base URL live on saved DB profiles."""
        _ = (name, protocol, base_url)

    def remove_custom_provider(self, name: str) -> None:
        """Remove a custom provider and its custom models. Saves."""
        providers = self._data.get("custom_providers", {})
        if isinstance(providers, dict) and name in providers:
            del providers[name]
        # Also remove custom models for this provider
        custom = self._data.get("custom_models", {})
        if isinstance(custom, dict) and name in custom:
            del custom[name]
        self._save()

    # ── build config for graph ───────────────────────────────────────────

    def build_config(self) -> Config:
        profile = self.resolve_profile()
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
            permission_mode=permission_mode,
            sandbox_mode=sandbox_mode,
            sandbox_workspace_write=self.get_sandbox_workspace_write(),
            approval_policy=approval_policy,
            approval_reviewer=approval_reviewer,
            ask_compact=bool(self._data.get("askCompact", self._data.get("ask_compact", False))),
        )

    def _get_profile(self, name: str) -> Profile | None:
        from voidx.memory.model_profiles import get_model_profile

        row = get_model_profile(name)
        if row is None:
            return None
        return Profile(
            name=row.name,
            api_key=row.api_key,
            base_url=row.base_url,
            protocol=row.protocol,
        )

    def _migrate_legacy_profiles(self) -> None:
        from voidx.memory.model_profiles import ModelProfileRow, save_model_profile

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
            save_model_profile(ModelProfileRow(
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
