"""MiniMax."""

from __future__ import annotations

from voidx.config import ModelConfig
from voidx.config.enums import ReasoningEffort
from voidx.llm.providers import base
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import resolve_effort


def _reasoning(config: ModelConfig) -> dict:
    """MiniMax format: ``extra_body.thinking.type`` + ``reasoning_split``."""
    effort = resolve_effort(config)
    if effort is ReasoningEffort.NONE:
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"extra_body": {"thinking": {"type": "enabled"}, "reasoning_split": True}}


base.register(ProviderSpec(
    name="minimax",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://api.minimax.io/v1",
    context_limit=1_000_000,
    static_models=(
        "MiniMax-M3",
        "MiniMax-M2.7",
        "MiniMax-M2.7-highspeed",
        "MiniMax-M2.5",
        "MiniMax-M2.5-highspeed",
    ),
    reasoning=_reasoning,
))
