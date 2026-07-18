"""Xunfei Astron Coding Plan — OpenAI-compatible proxy."""

from __future__ import annotations

from voidx.config import ModelConfig
from voidx.llm.providers import base
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.common import openai_effort


def _reasoning(config: ModelConfig) -> dict:
    """Same nested reasoning format as OpenRouter."""
    effort = openai_effort(config.reasoning_effort)
    if effort is None:
        return {}
    return {"extra_body": {"reasoning": {"effort": effort}}}


base.register(ProviderSpec(
    name="xunfei-coding-plan",
    protocol="openai",
    default_base_url="https://maas-coding-api.cn-huabei-1.xf-yun.com/v2",
    context_limit=200_000,
    static_models=(
        "astron-code-latest",
    ),
    reasoning=_reasoning,
))
