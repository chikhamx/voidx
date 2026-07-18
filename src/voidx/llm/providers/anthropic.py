"""Anthropic (first-party Claude API)."""

from __future__ import annotations

from voidx.config import ModelConfig
from voidx.llm.providers import base
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.common import ANTHROPIC_BUDGETS, normalized_effort


def _supports_effort(model: str) -> bool:
    return "claude-opus-4-" in model.lower()


def anthropic_reasoning(config: ModelConfig) -> dict:
    """Return Anthropic-compatible reasoning kwargs for first-party Claude models."""
    effort = normalized_effort(config.reasoning_effort)
    if effort in (None, "none"):
        return {}
    if _supports_effort(config.model):
        level = {"minimal": "low"}.get(effort, effort)
        return {"thinking": {"type": "adaptive"}, "effort": level}
    budget = ANTHROPIC_BUDGETS.get(effort, ANTHROPIC_BUDGETS["high"])
    budget = min(budget, max(config.max_tokens - 1, 1))
    if budget < 1_024:
        return {}
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}


base.register(ProviderSpec(
    name="anthropic",
    protocol="anthropic",
    default_base_url="https://api.anthropic.com",
    context_limit=200_000,
    static_models=(
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5",
    ),
    reasoning=anthropic_reasoning,
))
