"""OpenRouter (multi-provider gateway, OpenAI-compatible)."""

from __future__ import annotations

from voidx.config import ModelConfig
from voidx.llm.providers import base
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.common import openai_effort


def _reasoning(config: ModelConfig) -> dict:
    """OpenRouter accepts the nested reasoning format for any model."""
    effort = openai_effort(config.reasoning_effort)
    if effort is None:
        return {}
    return {"extra_body": {"reasoning": {"effort": effort}}}


base.register(ProviderSpec(
    name="openrouter",
    protocol="openai",
    default_base_url="https://openrouter.ai/api/v1",
    context_limit=128_000,
    reasoning=_reasoning,
))
