"""Pydantic configuration models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AiApprovalConfig(BaseModel):
    profile_name: str = ""
    timeout_seconds: float = Field(default=12.0, ge=1.0, le=60.0, allow_inf_nan=False)

from voidx.config.defaults import DEFAULT_MODEL, DEFAULT_PROVIDER
from voidx.config.enums import PermissionMode, ReasoningEffort
from voidx.mcp.domain.config import McpServerConfig
from voidx.platform.retry_config import RetryConfig

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
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    base_url: str | None = None
    protocol: str | None = None  # "openai" | "anthropic" | "gemini" | None (auto-detect)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=8192, ge=1, le=128000)
    reasoning_effort: ReasoningEffort = Field(
        default=ReasoningEffort.XHIGH,
        description="Reasoning intensity: none, low, medium, high, xhigh, max",
    )
    context_window: int | None = Field(
        default=None,
        ge=1,
        description="Override context window size in tokens. None = auto-detect by provider.",
    )


class AgentConfig(BaseModel):
    name: str
    description: str = ""
    model: ModelConfig | None = None
    tools: set[str] | None = None


class WebToolRoute(BaseModel):
    backend: str = "legacy"  # "legacy" | "mcp"
    server: str = ""
    tool: str = ""


class UserProfile(BaseModel):
    language: str = ""
    tone: str = ""


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=lambda: AgentConfig(
        name="build", description="Primary coding agent.",
    ))
    lsp_format_after_edit: bool = True
    workspace: str = "."
    permission_mode: PermissionMode = PermissionMode.SAFE
    sandbox_readable_files: list[str] = Field(
        default_factory=list,
        description="Extra file paths readable under workspace-write sandbox mode.",
    )
    sandbox_readable_dirs: list[str] = Field(
        default_factory=list,
        description="Extra directory paths readable under workspace-write sandbox mode.",
    )
    sandbox_writable_files: list[str] = Field(
        default_factory=list,
        description="Extra file paths writable under workspace-write sandbox mode.",
    )
    sandbox_writable_dirs: list[str] = Field(
        default_factory=list,
        description="Extra directory paths writable under workspace-write sandbox mode.",
    )
    persistent_readable_files: list[str] = Field(default_factory=list)
    persistent_readable_dirs: list[str] = Field(default_factory=list)
    persistent_writable_files: list[str] = Field(default_factory=list)
    persistent_writable_dirs: list[str] = Field(default_factory=list)
    ask_compact: bool = False
    compaction_soft_ratio: float = Field(default=0.75, ge=0.1, le=0.95)
    compaction_post_target_ratio: float = Field(default=0.10, ge=0.05, le=0.80)
    inline_compaction_enabled: bool = False
    user_profile: UserProfile = Field(default_factory=UserProfile)
    log_llm_exchange: bool = Field(
        default=False,
        description="Log full LLM request/response exchanges to llm_requests.jsonl.",
    )
    log_llm_diagnostic: bool = Field(
        default=True,
        description="Log goal-resolver diagnostic events to llm_requests.jsonl.",
    )

class SkillSelectionConfig(BaseModel):
    enabled: set[str] = Field(default_factory=set)
    disabled: set[str] = Field(default_factory=set)
    auto: set[str] = Field(default_factory=set)
