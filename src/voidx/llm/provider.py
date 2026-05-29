"""LLM Provider layer — typed abstraction over LangChain ChatModels."""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk

from voidx.config import ModelConfig


# ── protocol resolution ───────────────────────────────────────────────────

_PROVIDER_PROTOCOLS: dict[str, str] = {
    "anthropic": "anthropic",
    "deepseek": "anthropic",
    "openai": "openai",
    "openrouter": "openai",
    "mimo": "anthropic",
    "qwen": "anthropic",
    "zhipu": "anthropic",
    "kimi": "anthropic",
    "doubao": "openai",
}

_DEFAULT_BASE_URLS: dict[tuple[str, str], str] = {
    ("anthropic", "anthropic"): "https://api.anthropic.com",
    ("openai", "openai"): "https://api.openai.com/v1",
    ("deepseek", "anthropic"): "https://api.deepseek.com/anthropic",
    ("deepseek", "openai"): "https://api.deepseek.com/v1",
    ("openrouter", "openai"): "https://openrouter.ai/api/v1",
    ("mimo", "openai"): "https://api.xiaomimimo.com/v1",
    ("mimo", "anthropic"): "https://api.xiaomimimo.com/anthropic",
    ("qwen", "openai"): "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ("qwen", "anthropic"): "https://dashscope.aliyuncs.com/apps/anthropic",
    ("zhipu", "openai"): "https://open.bigmodel.cn/api/paas/v4",
    ("zhipu", "anthropic"): "https://open.bigmodel.cn/api/anthropic",
    ("kimi", "openai"): "https://api.moonshot.cn/v1",
    ("kimi", "anthropic"): "https://api.moonshot.cn/anthropic",
    ("doubao", "openai"): "https://ark.cn-beijing.volces.com/api/v3",
}


def resolve_protocol(config: ModelConfig) -> str:
    if config.protocol:
        return config.protocol
    return _PROVIDER_PROTOCOLS.get(config.provider, "openai")


# ── model factory ─────────────────────────────────────────────────────────

def create_chat_model(api_key: str, config: ModelConfig) -> BaseChatModel:
    protocol = resolve_protocol(config)
    base_url = config.base_url or _DEFAULT_BASE_URLS.get(
        (config.provider, protocol), ""
    )

    if protocol == "anthropic":
        kwargs = dict(
            api_key=api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        if base_url:
            kwargs["base_url"] = base_url
        return ChatAnthropic(**kwargs)

    if protocol == "openai":
        kwargs = dict(
            api_key=api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    if protocol == "gemini":
        raise NotImplementedError(
            "Gemini protocol not yet supported. Use 'openai' or 'anthropic'."
        )

    raise ValueError(f"Unknown protocol: {protocol}")


# ── thinking extraction ───────────────────────────────────────────────────

_THINKING_BLOCK_TYPES = {
    "thinking",
    "redacted_thinking",
    "reasoning",
    "reasoning_content",
}


def _extract_reasoning_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_extract_reasoning_text(item) for item in value)
    if not isinstance(value, dict):
        return ""

    parts: list[str] = []
    for key in ("thinking", "reasoning_content", "reasoning", "text", "data"):
        field = value.get(key)
        if isinstance(field, str):
            parts.append(field)

    summary = value.get("summary")
    if isinstance(summary, (dict, list)):
        parts.append(_extract_reasoning_text(summary))

    return "".join(parts)


def _extract_reasoning_blocks(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in _THINKING_BLOCK_TYPES:
            parts.append(_extract_reasoning_text(item))
    return "".join(parts)


def _extract_thinking_anthropic(chunk: AIMessageChunk) -> str:
    parts: list[str] = []
    content_text = _extract_reasoning_blocks(chunk.content)
    if content_text:
        parts.append(content_text)

    meta = chunk.response_metadata
    if isinstance(meta, dict):
        for key in ("thinking", "reasoning"):
            text = _extract_reasoning_text(meta.get(key))
            if text:
                parts.append(text)
    return "".join(parts)


def _extract_thinking_openai(chunk: AIMessageChunk) -> str:
    parts: list[str] = []
    content_text = _extract_reasoning_blocks(chunk.content)
    if content_text:
        parts.append(content_text)

    extra = chunk.additional_kwargs
    if isinstance(extra, dict):
        for key in ("reasoning_content", "reasoning", "reasoning_details"):
            text = _extract_reasoning_text(extra.get(key))
            if text:
                parts.append(text)
    return "".join(parts)


def extract_thinking(chunk: AIMessageChunk, protocol: str) -> str:
    if protocol == "anthropic":
        return _extract_thinking_anthropic(chunk)
    if protocol == "openai":
        return _extract_thinking_openai(chunk)
    return _extract_thinking_anthropic(chunk) or _extract_thinking_openai(chunk)


# ── context limits ────────────────────────────────────────────────────────

def get_context_limit(provider: str) -> int:
    limits: dict[str, int] = {
        "deepseek": 1_000_000,
        "anthropic": 200_000,
        "openai": 128_000,
        "openrouter": 128_000,
        "mimo": 128_000,
        "qwen": 128_000,
        "zhipu": 256_000,
        "kimi": 256_000,
        "doubao": 128_000,
    }
    return limits.get(provider, 128_000)
