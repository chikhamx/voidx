"""Configuration shim — wraps voidx_core Rust types with original Python API."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# ── Enums (mirrored from Rust) ─────────────────────────────────────────

class SandboxMode(str, Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ApprovalPolicy(str, Enum):
    UNTRUSTED = "untrusted"
    ON_FAILURE = "on-failure"
    ON_REQUEST = "on-request"
    NEVER = "never"


class ApprovalReviewer(str, Enum):
    USER = "user"
    AUTO_REVIEW = "auto_review"


class PermissionMode(str, Enum):
    DEFAULT = "default"
    READ_ONLY = "read-only"
    ACCEPT_EDITS = "accept-edits"
    AUTO_REVIEW = "auto-review"
    FULL_ACCESS = "full-access"
    CUSTOM = "custom"


class CodeIde(str, Enum):
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


# ── Config dataclasses ─────────────────────────────────────────────────

@dataclass
class ModelConfig:
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5"
    protocol: str | None = None
    base_url: str | None = None
    temperature: float = 0.7
    max_tokens: int = 8192
    reasoning_effort: str | None = None


@dataclass
class Profile:
    name: str = ""
    provider: str = ""
    model: str = ""
    api_key: str | None = None
    base_url: str | None = None


@dataclass
class Config:
    workspace: str = "."
    model: ModelConfig = field(default_factory=ModelConfig)
    sandbox_mode: SandboxMode = SandboxMode.WORKSPACE_WRITE
    sandbox_workspace_write: bool = False
    approval_policy: ApprovalPolicy = ApprovalPolicy.UNTRUSTED
    approval_reviewer: ApprovalReviewer = ApprovalReviewer.USER
    permission_mode: PermissionMode = PermissionMode.DEFAULT


# ── Settings (reads .voidx/settings.json) ──────────────────────────────

SETTINGS_FILE = ".voidx/settings.json"


class Settings:
    """Reads voidx settings from .voidx/settings.json."""

    def __init__(self, workspace: str = "."):
        self._workspace = workspace
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        path = Path(self._workspace) / SETTINGS_FILE
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def build_config(self) -> Config:
        """Build a Config from settings + env vars."""
        model_data = self._data.get("model", {})
        cfg = Config(
            workspace=self._workspace,
            model=ModelConfig(
                provider=model_data.get("provider", "anthropic"),
                model=model_data.get("model", "claude-haiku-4-5"),
                protocol=model_data.get("protocol"),
                base_url=model_data.get("base_url"),
                temperature=model_data.get("temperature", 0.7),
                max_tokens=model_data.get("max_tokens", 8192),
                reasoning_effort=model_data.get("reasoning_effort"),
            ),
        )
        sandbox = self._data.get("sandbox", {})
        if sandbox.get("mode"):
            cfg.sandbox_mode = SandboxMode(sandbox["mode"])
        cfg.sandbox_workspace_write = sandbox.get("workspace_write", False)

        approval = self._data.get("approval", {})
        if approval.get("policy"):
            cfg.approval_policy = ApprovalPolicy(approval["policy"])

        return cfg

    def resolve_api_key(self, provider: str) -> str | None:
        """Resolve API key from settings, env vars, or profile."""
        # Check settings.json
        apis = self._data.get("apis", {})
        for entry in apis:
            if entry.get("provider") == provider:
                key = entry.get("key", "")
                if key and not key.startswith("$"):
                    return key

        # Check env vars
        env_key = f"{provider.upper()}_API_KEY"
        if env_val := os.environ.get(env_key):
            return env_val

        return None

    def resolve_profile(self) -> Profile | None:
        """Return the default profile from settings, if configured."""
        profiles = self._data.get("profiles", [])
        default_name = self._data.get("default_profile")
        if not profiles:
            return None

        target = None
        if default_name:
            for p in profiles:
                if p.get("name") == default_name:
                    target = p
                    break
        if target is None:
            target = profiles[0]

        return Profile(
            name=target.get("name", ""),
            provider=target.get("provider", ""),
            model=target.get("model", ""),
            api_key=target.get("api_key"),
            base_url=target.get("base_url"),
        )

    def get_skill_selection(self) -> dict:
        return self._data.get("skills", {})

    def list_custom_models(self, provider: str) -> list[str]:
        custom = self._data.get("custom_models", {})
        return custom.get(provider, [])
