"""Mimo (Xiaomi) — registers both the standard and token-plan endpoints."""

from __future__ import annotations

import voidx.llm.providers.base as base
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import thinking_toggle

_STATIC_MODELS = (
    "mimo-v2.5-pro",
    "mimo-v2.5",
    "mimo-v2.5-tts",
)

base.register(ProviderSpec(
    name="mimo",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://api.xiaomimimo.com/v1",
    context_limit=1_000_000,
    static_models=_STATIC_MODELS,
    reasoning=thinking_toggle,
))

base.register(ProviderSpec(
    name="mimo-token-plan",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://token-plan-cn.xiaomimimo.com/v1",
    context_limit=1_000_000,
    static_models=_STATIC_MODELS,
    reasoning=thinking_toggle,
))
