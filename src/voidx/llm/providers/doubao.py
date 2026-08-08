"""Doubao (ByteDance Ark)."""

from __future__ import annotations

from voidx.llm.domain.model import ModelConfig
from voidx.llm.domain.model import ReasoningEffort
import voidx.llm.providers.base as base
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import resolve_effort

_DOUBAO_THINKING_MODELS = (
    "doubao-seed",
    "seed-1.6",
)


def _supports_thinking(model: str) -> bool:
    name = model.lower()
    return any(p in name for p in _DOUBAO_THINKING_MODELS)


def _reasoning(config: ModelConfig) -> dict:
    """Doubao format: ``extra_body.thinking.type`` (model-gated; no external auto)."""
    if not _supports_thinking(config.model):
        return {}
    effort = resolve_effort(config)
    thinking_type = "disabled" if effort is ReasoningEffort.NONE else "enabled"
    return {"extra_body": {"thinking": {"type": thinking_type}}}


base.register(ProviderSpec(
    name="doubao",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://ark.cn-beijing.volces.com/api/v3",
    context_limit=256_000,
    static_models=(
        "doubao-seed-1.6-thinking",
        "doubao-seed-1.6",
        "doubao-seed-1.6-flash",
    ),
    reasoning=_reasoning,
))
