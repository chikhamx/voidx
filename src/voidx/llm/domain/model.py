"""LLM model configuration owned by the LLM domain."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-4-6"


class ReasoningEffort(StrEnum):
    """Unified reasoning intensity for all providers and models."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ModelConfig(BaseModel):
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    base_url: str | None = None
    protocol: str | None = None
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
