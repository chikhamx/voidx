"""Qwen (Alibaba DashScope, OpenAI-compatible mode)."""

from __future__ import annotations

from voidx.config import ModelConfig
from voidx.llm.providers import base
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import ANTHROPIC_BUDGETS, normalized_effort

_QWEN_THINKING_MODELS = (
    "qwen3",
    "qwq",
)


def _supports_thinking(model: str) -> bool:
    return model.lower().startswith(_QWEN_THINKING_MODELS)


def _reasoning(config: ModelConfig) -> dict:
    """Qwen format: ``extra_body.enable_thinking`` + ``thinking_budget`` (model-gated)."""
    effort = normalized_effort(config.reasoning_effort)
    if effort is None:
        return {}
    if not _supports_thinking(config.model):
        return {}
    if effort == "none":
        return {"extra_body": {"enable_thinking": False}}
    budget = ANTHROPIC_BUDGETS.get(effort, ANTHROPIC_BUDGETS["high"])
    budget = min(budget, max(config.max_tokens - 1, 1))
    return {"extra_body": {"enable_thinking": True, "thinking_budget": budget}}


base.register(ProviderSpec(
    name="qwen",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    context_limit=1_000_000,
    static_models=(
        "qwen3.7-max",
        "qwen3-max",
        "qwen3.6-plus",
        "qwen-plus",
        "qwen-turbo",
    ),
    reasoning=_reasoning,
))
