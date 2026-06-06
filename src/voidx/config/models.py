"""Pydantic configuration models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.config.enums import ApprovalPolicy, ApprovalReviewer, PermissionMode, SandboxMode

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
