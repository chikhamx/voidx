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
    "mimo-token-plan": "anthropic",
    "qwen": "anthropic",
    "zhipu": "anthropic",
    "kimi": "anthropic",
    "doubao": "openai",
    "typex": "openai",
}

_DEFAULT_BASE_URLS: dict[tuple[str, str], str] = {
    ("anthropic", "anthropic"): "https://api.anthropic.com",
    ("openai", "openai"): "https://api.openai.com/v1",
    ("deepseek", "anthropic"): "https://api.deepseek.com/anthropic",
    ("deepseek", "openai"): "https://api.deepseek.com/v1",
    ("openrouter", "openai"): "https://openrouter.ai/api/v1",
    ("mimo", "openai"): "https://api.xiaomimimo.com/v1",
    ("mimo", "anthropic"): "https://api.xiaomimimo.com/anthropic",
    ("mimo-token-plan", "openai"): "https://token-plan-cn.xiaomimimo.com/v1",
    ("mimo-token-plan", "anthropic"): "https://token-plan-cn.xiaomimimo.com/anthropic",
    ("qwen", "openai"): "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ("qwen", "anthropic"): "https://dashscope.aliyuncs.com/apps/anthropic",
    ("zhipu", "openai"): "https://open.bigmodel.cn/api/paas/v4",
    ("zhipu", "anthropic"): "https://open.bigmodel.cn/api/anthropic",
    ("kimi", "openai"): "https://api.moonshot.cn/v1",
    ("kimi", "anthropic"): "https://api.moonshot.cn/anthropic",
    ("doubao", "openai"): "https://ark.cn-beijing.volces.com/api/v3",
    ("typex", "openai"): "https://newapi.typex-test.cn/v1",
}


def resolve_protocol(config: ModelConfig) -> str:
    if config.protocol:
        return config.protocol
    return _PROVIDER_PROTOCOLS.get(config.provider, "openai")


# ── model factory ─────────────────────────────────────────────────────────

# ── reasoning effort mapping ─────────────────────────────────────────────

_ANTHROPIC_BUDGETS = {
    "low": 1_024,
    "medium": 4_096,
    "high": 8_192,
}

_REASONING_PREFIXES = (
    "gpt-5",
    "o1",
    "o3",
    "o4",
)


def _normalized_effort(effort: str | None) -> str | None:
    if effort is None:
        return None
    value = effort.strip().lower()
    if value in {"", "off", "none"}:
        return "none"
    if value in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        return value
    return "medium"


def _supports_openai_reasoning(model: str) -> bool:
    name = model.lower()
    return name.startswith(_REASONING_PREFIXES)


def _supports_anthropic_effort(model: str) -> bool:
    name = model.lower()
    return "claude-opus-4-" in name


def _anthropic_reasoning_kwargs(config: ModelConfig) -> dict:
    """Return Anthropic-compatible reasoning kwargs for first-party Claude models."""
    effort = _normalized_effort(config.reasoning_effort)
    if effort in (None, "none"):
        return {}
    if _supports_anthropic_effort(config.model):
        level = {"minimal": "low"}.get(effort, effort)
        return {"thinking": {"type": "adaptive"}, "effort": level}
    budget = _ANTHROPIC_BUDGETS.get(effort, _ANTHROPIC_BUDGETS["high"])
    budget = min(budget, max(config.max_tokens - 1, 1))
    if budget < 1_024:
        return {}
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}


def _mimo_reasoning_kwargs(effort: str | None) -> dict:
    value = _normalized_effort(effort)
    if value == "none":
        return {"thinking": {"type": "disabled"}}
    if value is None:
        return {}
    return {"thinking": {"type": "enabled"}}


def _openai_reasoning_effort(effort: str | None) -> str | None:
    """Map unified reasoning_effort to OpenAI reasoning_effort string."""
    value = _normalized_effort(effort)
    if value is None:
        return None
    return {"none": "none", "minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "high"}.get(value)


def _openai_reasoning_kwargs(config: ModelConfig) -> dict:
    effort = _openai_reasoning_effort(config.reasoning_effort)

    if config.provider == "openai":
        if effort is None:
            return {}
        if not _supports_openai_reasoning(config.model):
            return {}
        if effort == "none":
            if config.model.lower().startswith("gpt-5"):
                return {"reasoning_effort": "none"}
            return {"reasoning_effort": "low"}
        return {"reasoning_effort": effort}

    if config.provider == "openrouter":
        if effort is None:
            return {}
        return {"extra_body": {"reasoning": {"effort": effort}}}

    if config.provider == "doubao":
        return _doubao_reasoning_kwargs(config)

    if config.provider == "typex":
        if effort is None:
            return {}
        return {"reasoning_effort": {"none": "none", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}.get(effort, "high")}

    return {}


def _doubao_reasoning_kwargs(config: ModelConfig) -> dict:
    if "thinking" not in config.model.lower() and "seed-1.6" not in config.model.lower():
        return {}
    effort = (config.reasoning_effort or "").strip().lower()
    if effort in {"", "off", "none"}:
        thinking_type = "disabled"
    elif effort == "auto":
        thinking_type = "auto"
    else:
        thinking_type = "enabled"
    return {"extra_body": {"thinking": {"type": thinking_type}}}


def _reasoning_kwargs(config: ModelConfig, protocol: str) -> dict:
    if protocol == "anthropic":
        if config.provider == "anthropic":
            return _anthropic_reasoning_kwargs(config)
        if config.provider in ("mimo", "deepseek", "mimo-token-plan"):
            return _mimo_reasoning_kwargs(config.reasoning_effort)
        return {}
    if protocol == "openai":
        return _openai_reasoning_kwargs(config)
    return {}


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
        kwargs.update(_reasoning_kwargs(config, protocol))
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
        kwargs.update(_reasoning_kwargs(config, protocol))
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

def get_context_limit(provider: str, protocol: str = "") -> int:
    """Return context-window limit for *provider*.  Falls back to *protocol* for unknown providers."""
    limits: dict[str, int] = {
        "deepseek": 1_000_000,
        "anthropic": 200_000,
        "openai": 1_050_000,
        "openrouter": 128_000,
        "mimo": 1_000_000,
        "mimo-token-plan": 1_000_000,
        "qwen": 1_000_000,
        "zhipu": 200_000,
        "kimi": 262_144,
        "doubao": 256_000,
        "typex": 128_000,
    }
    if provider in limits:
        return limits[provider]
    if protocol == "anthropic":
        return 200_000
    return 128_000
