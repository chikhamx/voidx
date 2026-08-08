"""Anthropic (first-party Claude API)."""

from __future__ import annotations

from voidx.llm.domain.model import ModelConfig
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.domain.model import ReasoningEffort
from voidx.llm.providers.common import ANTHROPIC_BUDGETS, map_effort, resolve_effort

# ChatAnthropic.effort only accepts these values for adaptive thinking.
_ANTHROPIC_ADAPTIVE_EFFORTS = (
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
    ReasoningEffort.MAX,
)


def _supports_adaptive(model: str) -> bool:
    name = model.lower()
    return any(
        p in name
        for p in (
            "claude-opus-4-",
            "claude-opus-5",
            "claude-sonnet-5",
        )
    )


def anthropic_reasoning(config: ModelConfig) -> dict:
    """Return Anthropic-compatible reasoning kwargs for first-party Claude models."""
    effort = resolve_effort(config)
    if effort is ReasoningEffort.NONE:
        return {}
    if _supports_adaptive(config.model):
        level = map_effort(effort, _ANTHROPIC_ADAPTIVE_EFFORTS)
        return {"thinking": {"type": "adaptive"}, "effort": level.value}
    budget = ANTHROPIC_BUDGETS.get(effort, ANTHROPIC_BUDGETS[ReasoningEffort.HIGH])
    budget = min(budget, max(config.max_tokens - 1, 1))
    if budget < 1_024:
        return {}
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}


SPEC = ProviderSpec(
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
)
