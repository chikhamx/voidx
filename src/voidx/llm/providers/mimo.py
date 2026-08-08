"""Mimo provider specs for the standard and token-plan endpoints."""

from __future__ import annotations

from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import thinking_toggle

_STATIC_MODELS = (
    "mimo-v2.5-pro",
    "mimo-v2.5",
    "mimo-v2.5-tts",
)

MIMO_SPEC = ProviderSpec(
    name="mimo",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://api.xiaomimimo.com/v1",
    context_limit=1_000_000,
    static_models=_STATIC_MODELS,
    reasoning=thinking_toggle,
)

MIMO_TOKEN_PLAN_SPEC = ProviderSpec(
    name="mimo-token-plan",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://token-plan-cn.xiaomimimo.com/v1",
    context_limit=1_000_000,
    static_models=_STATIC_MODELS,
    reasoning=thinking_toggle,
)

SPECS = (MIMO_SPEC, MIMO_TOKEN_PLAN_SPEC)
