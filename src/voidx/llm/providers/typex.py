"""Typex (third-party relay serving GLM models)."""

from __future__ import annotations

from voidx.config import ModelConfig
from voidx.llm.providers import base
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import normalized_effort, supports_zhipu_thinking


def _reasoning(config: ModelConfig) -> dict:
    """Same thinking schema as zhipu (typex serves GLM models)."""
    effort = normalized_effort(config.reasoning_effort)
    if effort is None:
        return {}
    if not supports_zhipu_thinking(config.model):
        return {}
    if effort == "none":
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"extra_body": {"thinking": {"type": "enabled"}}}


base.register(ProviderSpec(
    name="typex",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://newapi.typex-test.cn/v1",
    context_limit=128_000,
    static_models=(
        "zai-org/GLM-5-FP8",
    ),
    reasoning=_reasoning,
))
