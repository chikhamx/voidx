"""Typex (third-party relay serving GLM models)."""

from __future__ import annotations

import voidx.llm.providers.base as base
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import zhipu_reasoning


base.register(ProviderSpec(
    name="typex",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://newapi.typex-test.cn/v1",
    context_limit=200_000,
    static_models=(
        "zai-org/GLM-5-FP8",
    ),
    reasoning=zhipu_reasoning,
))
