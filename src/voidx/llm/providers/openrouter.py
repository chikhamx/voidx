"""OpenRouter (multi-provider gateway, OpenAI-compatible)."""

from __future__ import annotations

import voidx.llm.providers.base as base
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.common import nested_reasoning


base.register(ProviderSpec(
    name="openrouter",
    protocol="openai",
    default_base_url="https://openrouter.ai/api/v1",
    context_limit=200_000,
    reasoning=nested_reasoning,
))
