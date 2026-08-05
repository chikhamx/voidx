"""OpenAI — first-party API and the openai-protocol fallback logic.

Also hosts :class:`ReasoningPreservingChatOpenAI`, the chat model class used
for every provider on the openai protocol (OpenRouter, custom relays), and
the stainless-header stripping needed by third-party relays.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from voidx.config import ModelConfig
from voidx.llm.providers import base
from voidx.llm.providers.base import ProviderSpec
from voidx.config.enums import ReasoningEffort
from voidx.llm.providers.common import openai_effort, preserve_reasoning_delta, resolve_effort

OFFICIAL_OPENAI_PROVIDERS = {"openai", "openrouter"}

OFFICIAL_OPENAI_BASE_URLS = {
    "https://api.openai.com",
    "https://api.openai.com/v1",
}

_STAINLESS_HEADERS_TO_STRIP = {
    "x-stainless-lang",
    "x-stainless-os",
    "x-stainless-arch",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-stainless-package-version",
    "x-stainless-async",
    "x-stainless-retry-count",
}


def strip_stainless_headers() -> dict[str, str]:
    """Return headers that clear OpenAI SDK fingerprint for third-party relays.

    Many third-party relays block requests carrying x-stainless-* headers
    to prevent unmodified SDK access.
    """
    return {k: "" for k in _STAINLESS_HEADERS_TO_STRIP} | {"User-Agent": "voidx/1.0"}


_REASONING_PREFIXES = (
    "gpt-5",
    "o1",
    "o3",
    "o4",
)


def supports_openai_reasoning(model: str) -> bool:
    name = model.lower()
    return name.startswith(_REASONING_PREFIXES)


def openai_reasoning(config: ModelConfig) -> dict:
    """Top-level ``reasoning_effort`` format for OpenAI reasoning models.

    OpenAI-compatible providers with their own request shape use dedicated
    provider hooks instead of this standard OpenAI implementation.
    """
    if not supports_openai_reasoning(config.model):
        return {}
    effort = openai_effort(
        config.reasoning_effort,
        provider=config.provider,
        model=config.model,
    )
    if effort == ReasoningEffort.NONE.value and not config.model.lower().startswith("gpt-5"):
        effort = ReasoningEffort.LOW.value
    return {"reasoning_effort": effort}


class ReasoningPreservingChatOpenAI(ChatOpenAI):
    """OpenAI-compatible chat model that preserves streaming reasoning deltas."""

    def _convert_chunk_to_generation_chunk(  # type: ignore[override]
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk is None:
            return None

        msg = generation_chunk.message
        if not isinstance(msg, AIMessageChunk):
            return generation_chunk

        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            preserve_reasoning_delta(msg, delta)

        return generation_chunk


def _temperature_override(config: ModelConfig) -> float | None:
    """OpenAI reasoning models (o1/o3/o4/gpt-5) require temperature = 1.0."""
    if supports_openai_reasoning(config.model):
        return 1.0
    return config.temperature


base.register(ProviderSpec(
    name="openai",
    protocol="openai",
    default_base_url="https://api.openai.com/v1",
    context_limit=1_050_000,
    static_models=(
        "gpt-5.5",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "o3",
        "o4-mini",
    ),
    reasoning=openai_reasoning,
    temperature_override=_temperature_override,
))
