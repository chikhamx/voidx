"""LongCat (Meituan)."""

from __future__ import annotations

from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import thinking_toggle

SPEC = ProviderSpec(
    name="longcat",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://api.longcat.chat/openai/v1",
    context_limit=200_000,
    static_models=(
        "LongCat-2.0",
    ),
    reasoning=thinking_toggle,
)
