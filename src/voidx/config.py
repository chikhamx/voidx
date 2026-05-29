"""Configuration system — typed, JSON-backed, no .env restrictions."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

SETTINGS_FILE = "voidx.json"


class Profile(BaseModel):
    """A named LLM configuration.  Name is ``provider/model`` (e.g. ``mimo/mimo-v2.5-pro``)."""
    name: str
    api_key: str = ""
    base_url: str | None = None

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

    @property
    def tool_count(self) -> int:
        if isinstance(self.tools, dict):
            return len(self.tools)
        if isinstance(self.tools, list):
            return len(self.tools)
        return 0


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=lambda: AgentConfig(
        name="build", description="Primary coding agent.",
    ))
    workspace: str = "."


# ── JSON-backed settings store ────────────────────────────────────────────

class Settings:
    """Persistent settings backed by ``voidx.json`` in the workspace directory."""

    def __init__(self, workspace: str = ".") -> None:
        self._workspace = Path(workspace).resolve()
        self._path = self._workspace / SETTINGS_FILE
        self._data: dict = self._load()
        self._runtime_keys: dict[str, str] = {}

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

    # ── profiles API ─────────────────────────────────────────────────────

    def list_profiles(self) -> list[Profile]:
        profiles_data = self._data.get("profiles", {})
        result: list[Profile] = []
        for name, fields in profiles_data.items():
            if isinstance(fields, dict) and fields.get("api_key"):
                result.append(Profile(
                    name=name,
                    api_key=fields["api_key"],
                    base_url=fields.get("base_url"),
                ))
        return result

    def resolve_profile(self, name: str = "") -> Profile | None:
        profiles = self.list_profiles()
        if not profiles:
            return None
        if not name:
            name = self._data.get("default_profile", "")
        if name:
            for p in profiles:
                if p.name == name:
                    return p
        return profiles[0] if profiles else None

    def save_profile(self, profile: Profile) -> Path:
        self._data.setdefault("profiles", {})[profile.name] = {
            "api_key": profile.api_key,
        }
        if profile.base_url:
            self._data["profiles"][profile.name]["base_url"] = profile.base_url
        self._data["default_profile"] = profile.name
        self._save()
        return self._path

    def delete_profile(self, name: str) -> Path:
        profiles = self._data.get("profiles", {})
        profiles.pop(name, None)
        if self._data.get("default_profile") == name:
            self._data["default_profile"] = next(iter(profiles), "") if profiles else ""
            if not self._data["default_profile"]:
                self._data.pop("default_profile", None)
        if not profiles:
            self._data.pop("profiles", None)
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

    # ── build config for graph ───────────────────────────────────────────

    def build_config(self) -> Config:
        profile = self.resolve_profile()
        if profile:
            provider = profile.provider
            model = profile.model
            base_url = profile.base_url
        else:
            provider = "anthropic"
            model = "claude-sonnet-4-6"
            base_url = None
        return Config(
            model=ModelConfig(provider=provider, model=model, base_url=base_url),
        )
