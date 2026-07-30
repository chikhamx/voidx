"""Kimi (Moonshot)."""

from __future__ import annotations

from voidx.config import ModelConfig
from voidx.config.enums import ReasoningEffort
from voidx.llm.providers import base
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import map_effort, resolve_effort, thinking_toggle

_KIMI_K3_EFFORTS = (
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.HIGH,
    ReasoningEffort.MAX,
)


def _reasoning(config: ModelConfig) -> dict:
    """Kimi format: ``extra_body.thinking.type``; k3 models also take ``reasoning_effort``."""
    if "k3" not in config.model.lower():
        return thinking_toggle(config)
    effort = map_effort(resolve_effort(config), _KIMI_K3_EFFORTS)
    if effort is ReasoningEffort.NONE:
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {
        "reasoning_effort": effort.value,
        "extra_body": {"thinking": {"type": "enabled"}},
    }


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
