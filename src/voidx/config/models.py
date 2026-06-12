"""Pydantic configuration models."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from voidx.config.enums import ApprovalPolicy, ApprovalReviewer, PermissionMode, SandboxMode

_NO_LIMIT_FALLBACK = 500

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
    tools: set[str] | None = None


class McpServerConfig(BaseModel):
    name: str
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    disabled: bool = False
    tools: list[str] | dict[str, object] | None = None
    transport: str = ""  # "stdio" | "sse" | "streamable-http"; auto-detected from url if blank

    @property
    def effective_transport(self) -> str:
        """Return the transport mode, auto-detecting from url when blank."""
        if self.transport:
            return self.transport
        if self.url:
            return "sse"
        return "stdio"

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


class UserProfile(BaseModel):
    language: str = ""
    tone: str = ""


class AgentMaxSteps(BaseModel):
    """Per-agent step limits and graph recursion limit.

    Step fields (voidx, explore, etc.) are convergence ceilings: the
    agent loop reserves the last step for a tool-free final answer, so
    max_steps=3 yields one tool-call step and one final answer step.
    Setting any step field to 0 normalizes it to _NO_LIMIT_FALLBACK (500).

    recursion_limit caps LangGraph node transitions. Each LLM step consumes
    ~2 recursions (call_llm → execute_tools → call_llm), so the effective
    limit is derived as max(recursion_limit, 2 * max_steps + 10) at runtime.
    """
    voidx: int = Field(default=100, ge=0, le=500)
    explore: int = Field(default=25, ge=0, le=500)
    plan: int = Field(default=30, ge=0, le=500)
    implement: int = Field(default=100, ge=0, le=500)
    review: int = Field(default=30, ge=0, le=500)
    compaction: int = Field(default=3, ge=0, le=500)
    title: int = Field(default=2, ge=0, le=500)
    recursion_limit: int = Field(default=500, ge=0, le=2000)

    @model_validator(mode="before")
    @classmethod
    def _normalize_zeros(cls, data: dict | object) -> dict | object:
        if not isinstance(data, dict):
            return data
        updates = {}
        for name, field_info in cls.model_fields.items():
            value = data.get(name)
            if value == 0:
                updates[name] = _NO_LIMIT_FALLBACK
        data.update(updates)
        return data


class ParallelSubagentsConfig(BaseModel):
    """Runtime gate for concurrent child-agent execution."""

    enabled: bool = False
    max_concurrent: int = Field(default=4, ge=1, le=8)


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=lambda: AgentConfig(
        name="build", description="Primary coding agent.",
    ))
    agent_max_steps: AgentMaxSteps = Field(default_factory=AgentMaxSteps)
    parallel_subagents: ParallelSubagentsConfig = Field(default_factory=ParallelSubagentsConfig)
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
    user_profile: UserProfile = Field(default_factory=UserProfile)
