"""Xunfei Astron Coding Plan — OpenAI-compatible proxy."""

from __future__ import annotations

from voidx.llm.providers import base
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.common import nested_reasoning


base.register(ProviderSpec(
    name="xunfei-coding-plan",
    protocol="openai",
    default_base_url="https://maas-coding-api.cn-huabei-1.xf-yun.com/v2",
    context_limit=200_000,
    static_models=(
        "astron-code-latest",
    ),
    reasoning=nested_reasoning,
))
