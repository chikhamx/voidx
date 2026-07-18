"""Zhipu (GLM / bigmodel.cn)."""

from __future__ import annotations

from voidx.config import ModelConfig
from voidx.llm.providers import base
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import normalized_effort, supports_zhipu_thinking


def _reasoning(config: ModelConfig) -> dict:
    """Zhipu format: ``extra_body.thinking.type`` (model-gated)."""
    effort = normalized_effort(config.reasoning_effort)
    if effort is None:
        return {}
    if not supports_zhipu_thinking(config.model):
        return {}
    if effort == "none":
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"extra_body": {"thinking": {"type": "enabled"}}}


base.register(ProviderSpec(
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
    reasoning=_reasoning,
))
