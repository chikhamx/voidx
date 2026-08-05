"""LLM Provider layer — protocol-level factory over LangChain ChatModels.

Per-provider metadata (default base URLs, static models, context limits,
reasoning hooks) lives in :mod:`voidx.llm.providers`; this module keeps the
protocol dispatch, the chat-model factory, thinking extraction, and the
stable import surface used by ``service.py`` and tests.

Four protocols:
  - ``anthropic``  — first-party Anthropic API
  - ``openai``     — OpenAI and OpenAI-compatible (OpenRouter, custom relays)
  - ``deepseek``   — China-domestic OpenAI-compatible providers (DeepSeek, Qwen,
                     Zhipu/GLM, Doubao, Mimo, Kimi, Typex, MiniMax, etc.)
  - ``gemini``     — Google Gemini native API (via langchain-google-genai)
"""

from __future__ import annotations

import os

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from voidx.config import ModelConfig
from voidx.llm.providers import get
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK
from voidx.llm.providers.anthropic import anthropic_reasoning as _anthropic_reasoning_kwargs
from voidx.llm.providers.deepseek import DeepSeekChatOpenAI
from voidx.llm.providers.gemini import (
    _is_gemini3_plus,
    ensure_gemini_dep as _ensure_gemini_dep,
    gemini_reasoning as _gemini_reasoning_kwargs,
    strip_gemini_version_suffix as _strip_gemini_version_suffix,
)
from voidx.llm.providers.openai import (
    OFFICIAL_OPENAI_BASE_URLS as _OFFICIAL_OPENAI_BASE_URLS,
    OFFICIAL_OPENAI_PROVIDERS as _OFFICIAL_OPENAI_PROVIDERS,
    ReasoningPreservingChatOpenAI,
    openai_reasoning as _openai_compatible_reasoning_kwargs,
    strip_stainless_headers as _strip_stainless_headers,
)


# ── protocol resolution ───────────────────────────────────────────────────


def resolve_protocol(config: ModelConfig) -> str:
    if config.protocol:
        return config.protocol
    spec = get(config.provider)
    return spec.protocol if spec is not None else "openai"


def _normalize_base_url(url: str) -> str:
    return url.strip().rstrip("/")


def _resolve_base_url(config: ModelConfig, protocol: str) -> str:
    spec = get(config.provider)
    stale_openai_base_url = (
        spec is not None
        and spec.protocol != "openai"
        and _normalize_base_url(config.base_url or "") in _OFFICIAL_OPENAI_BASE_URLS
    )
    if config.base_url and not stale_openai_base_url:
        return config.base_url
    if spec is not None:
        return spec.default_base_url
    return ""


# ── reasoning dispatch ────────────────────────────────────────────────────


def _reasoning_kwargs(config: ModelConfig, protocol: str) -> dict:
    spec = get(config.provider)
    if spec is not None:
        return spec.reasoning(config) if spec.reasoning is not None else {}
    if protocol == "anthropic":
        return _anthropic_reasoning_kwargs(config)
    if protocol == "gemini":
        return _gemini_reasoning_kwargs(config)
    if protocol == PROTOCOL_DEEPSEEK:
        return DeepSeekChatOpenAI.reasoning_kwargs(config)
    if protocol == "openai":
        return _openai_compatible_reasoning_kwargs(config)
    return {}


_STREAM_CHUNK_TIMEOUT_ENV = "LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S"
_REASONING_STREAM_CHUNK_TIMEOUT = 600


def _reasoning_stream_chunk_timeout(reasoning_kwargs: dict) -> int | None:
    if _STREAM_CHUNK_TIMEOUT_ENV in os.environ or not reasoning_kwargs:
        return None

    if reasoning_kwargs.get("reasoning_effort") == "none":
        return None

    extra_body = reasoning_kwargs.get("extra_body")
    if isinstance(extra_body, dict):
        if extra_body.get("enable_thinking") is False:
            return None
        thinking = extra_body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "disabled":
            return None
        reasoning = extra_body.get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("effort") == "none":
            return None

    return _REASONING_STREAM_CHUNK_TIMEOUT


_REASONING_MODEL_FIELDS = (
    "thinking",
    "effort",
    "reasoning_effort",
    "thinking_budget",
    "thinking_level",
    "include_thoughts",
)
_REASONING_EXTRA_BODY_KEYS = {
    "enable_thinking",
    "thinking_budget",
    "thinking",
    "reasoning",
    "reasoning_split",
    "reasoning_effort",
}


def create_resolver_model(
    model: BaseChatModel,
    config: ModelConfig,
) -> BaseChatModel:
    """Copy a chat model with reasoning disabled or provider-minimized."""
    model_copy = getattr(model, "model_copy", None)
    if not callable(model_copy):
        return model

    resolver_config = config.model_copy(update={"reasoning_effort": "none"})
    reasoning_kwargs = dict(_reasoning_kwargs(
        resolver_config,
        resolve_protocol(resolver_config),
    ))
    model_fields = getattr(type(model), "model_fields", {})
    updates: dict = {
        field: None
        for field in _REASONING_MODEL_FIELDS
        if field in model_fields
    }

    if "extra_body" in model_fields:
        extra_body = {
            key: value
            for key, value in dict(getattr(model, "extra_body", None) or {}).items()
            if key not in _REASONING_EXTRA_BODY_KEYS
        }
        resolver_extra_body = reasoning_kwargs.pop("extra_body", None)
        if isinstance(resolver_extra_body, dict):
            extra_body.update(resolver_extra_body)
        updates["extra_body"] = extra_body or None

    for key, value in reasoning_kwargs.items():
        if key in model_fields:
            updates[key] = value

    return model_copy(update=updates)


# ── model factory ─────────────────────────────────────────────────────────


def create_chat_model(api_key: str, config: ModelConfig) -> BaseChatModel:
    protocol = resolve_protocol(config)
    base_url = _resolve_base_url(config, protocol)

    # Resolve temperature: delegate to provider spec when it defines a
    # temperature_override hook (e.g. deepseek-reasoner → None, kimi → 1.0,
    # openai reasoning models → 1.0).  Fall back to config.temperature.
    spec = get(config.provider)
    if spec is not None and spec.temperature_override is not None:
        temp = spec.temperature_override(config)
    else:
        temp = config.temperature

    if protocol == "anthropic":
        kwargs = dict(
            api_key=api_key,
            model=config.model,
            max_tokens=config.max_tokens,
        )
        if temp is not None:
            kwargs["temperature"] = temp
        if base_url:
            kwargs["base_url"] = base_url
        kwargs.update(_reasoning_kwargs(config, protocol))
        return ChatAnthropic(**kwargs)

    if protocol == PROTOCOL_DEEPSEEK:
        reasoning_kwargs = _reasoning_kwargs(config, protocol)
        kwargs = dict(
            api_key=api_key,
            model=config.model,
            max_tokens=config.max_tokens,
        )
        if temp is not None:
            kwargs["temperature"] = temp
        stream_chunk_timeout = _reasoning_stream_chunk_timeout(reasoning_kwargs)
        if stream_chunk_timeout is not None:
            kwargs["stream_chunk_timeout"] = stream_chunk_timeout
        if base_url:
            kwargs["base_url"] = base_url
        kwargs.update(reasoning_kwargs)
        return DeepSeekChatOpenAI(**kwargs)

    if protocol == "openai":
        reasoning_kwargs = _reasoning_kwargs(config, protocol)
        kwargs = dict(
            api_key=api_key,
            model=config.model,
            max_tokens=config.max_tokens,
        )
        if temp is not None:
            kwargs["temperature"] = temp
        if base_url:
            kwargs["base_url"] = base_url
        if config.provider not in _OFFICIAL_OPENAI_PROVIDERS:
            kwargs["default_headers"] = _strip_stainless_headers()
        stream_chunk_timeout = _reasoning_stream_chunk_timeout(reasoning_kwargs)
        if stream_chunk_timeout is not None:
            kwargs["stream_chunk_timeout"] = stream_chunk_timeout
        kwargs.update(reasoning_kwargs)
        return ReasoningPreservingChatOpenAI(**kwargs)

    if protocol == "gemini":
        _ensure_gemini_dep()
        from langchain_google_genai import ChatGoogleGenerativeAI
        kwargs = dict(
            model=config.model,
            max_tokens=config.max_tokens,
        )
        if temp is not None:
            kwargs["temperature"] = temp
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = _strip_gemini_version_suffix(base_url)
        kwargs.update(_reasoning_kwargs(config, protocol))
        return ChatGoogleGenerativeAI(**kwargs)

    raise ValueError(f"Unknown protocol: {protocol}")


# ── thinking extraction ───────────────────────────────────────────────────
# Moved to voidx.llm.thinking; re-exported here for import compatibility.

from voidx.llm.thinking import extract_thinking  # noqa: E402,F401


# ── context limits ────────────────────────────────────────────────────────

def get_context_limit(provider: str, protocol: str = "", context_window: int | None = None) -> int:
    """Return context-window limit for *provider*.  Falls back to *protocol* for unknown providers."""
    if context_window is not None and context_window > 0:
        return context_window
    spec = get(provider)
    if spec is not None and spec.context_limit:
        return spec.context_limit
    if protocol == "anthropic":
        return 200_000
    return 128_000
