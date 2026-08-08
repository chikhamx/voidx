"""Zhipu (GLM / bigmodel.cn)."""

from __future__ import annotations

from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import zhipu_reasoning




SPEC = ProviderSpec(
    name="zhipu",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://open.bigmodel.cn/api/paas/v4",
    context_limit=200_000,
    static_models=(
        "glm-5.1",
        "glm-5",
        "glm-4.7",
        "glm-4.7-flash",
    ),
    reasoning=zhipu_reasoning,
)
