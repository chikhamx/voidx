"""Kimi (Moonshot)."""

from __future__ import annotations

from voidx.config import ModelConfig
from voidx.llm.providers import base
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import normalized_effort


def _reasoning(config: ModelConfig) -> dict:
    """Kimi format: ``extra_body.thinking.type``; k3 models also take ``reasoning_effort``."""
    effort = normalized_effort(config.reasoning_effort)
    if effort is None:
        return {}
    if effort == "none":
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    if "k3" in config.model.lower():
        raw_effort = (config.reasoning_effort or "").strip().lower()
        if raw_effort in ("ultra", "max", "xhigh"):
            mapped = "max"
        elif raw_effort in ("high", "medium"):
            mapped = "high"
        elif raw_effort in ("low", "minimum", "light", "minimal"):
            mapped = "low"
        else:
            if effort in ("xhigh", "max"):
                mapped = "max"
            elif effort in ("high", "medium"):
                mapped = "high"
            elif effort in ("low", "minimal"):
                mapped = "low"
            else:
                mapped = "max"
        return {"reasoning_effort": mapped, "extra_body": {"thinking": {"type": "enabled"}}}
    return {"extra_body": {"thinking": {"type": "enabled"}}}


def _temperature_override(config: ModelConfig) -> float | None:
    """Kimi models require temperature = 1.0."""
    return 1.0


base.register(ProviderSpec(
    name="kimi",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://api.kimi.com/coding/v1",
    context_limit=262_144,
    static_models=(
        "k3",
        "kimi-for-coding",
        "kimi-for-coding-highspeed",
        "kimi-k2.6",
        "kimi-k2.5",
        "kimi-k2",
    ),
    reasoning=_reasoning,
    temperature_override=_temperature_override,
))
