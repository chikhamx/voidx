"""Configuration system — every setting is typed, no loose strings."""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelConfig(BaseModel):
    """A specific model configuration."""
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    base_url: str | None = None  # custom endpoint for compatible APIs
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=8192, ge=1, le=128000)


class AgentConfig(BaseModel):
    """Agent-level configuration — mirrors opencode agent Info."""
    name: str
    description: str = ""
    model: ModelConfig | None = None
    max_steps: int = Field(default=50, ge=1, le=500)
    # Each orchestrator step consumes ~2 graph node visits (call_llm + execute_tools/finalize),
    # so recursion_limit must be at least max_steps * 2 + buffer. Default of 200 safely
    # covers the default max_steps=50 (needs ~105) plus sub-agent steps and re-triggers.
    recursion_limit: int = Field(default=200, ge=1, le=1000)
    tools: set[str] | None = None  # None = all tools


class Config(BaseModel):
    """Runtime configuration, derived from Settings and CLI args."""
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=lambda: AgentConfig(
        name="build",
        description="Primary coding agent.",
    ))
    workspace: str = "."  # current working directory


class Settings(BaseSettings):
    """Environment-sourced settings. Loaded from .env and env vars."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="VOIDX_",
        extra="ignore",
    )

    default_provider: str = "anthropic"
    default_model: str = "claude-sonnet-4-6"

    # API keys
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str | None = Field(default=None, alias="ANTHROPIC_BASE_URL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")

    def resolve_api_key(self, provider: str) -> str | None:
        # deepseek uses the anthropic key with a custom base URL
        if provider == "deepseek":
            return self.anthropic_api_key
        key_map: dict[str, str | None] = {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "openrouter": self.openrouter_api_key,
        }
        return key_map.get(provider)

    def build_config(self) -> Config:
        provider = self.default_provider
        base_url: str | None = None
        if provider == "deepseek":
            base_url = self.anthropic_base_url
        elif provider == "openrouter":
            base_url = "https://openrouter.ai/api/v1"
        return Config(
            model=ModelConfig(
                provider=provider,
                model=self.default_model,
                base_url=base_url,
            ),
        )
