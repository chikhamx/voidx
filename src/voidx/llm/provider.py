"""LLM Provider layer — typed abstraction over LangChain ChatModels.

Three protocols:
  - ``anthropic``  — first-party Anthropic API
  - ``openai``     — OpenAI and OpenAI-compatible (OpenRouter, custom relays)
  - ``deepseek``   — China-domestic OpenAI-compatible providers (DeepSeek, Qwen,
                     Zhipu/GLM, Doubao, Mimo, Kimi, Typex, MiniMax, etc.)

The ``deepseek`` protocol uses :class:`DeepSeekChatOpenAI`, a ``ChatOpenAI``
subclass that preserves ``reasoning_content`` in streaming chunks (LangChain
silently drops it) and handles provider-specific reasoning-effort mapping.
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from voidx.config import ModelConfig


# ── protocol resolution ───────────────────────────────────────────────────

_PROVIDER_PROTOCOLS: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "openrouter": "openai",
    # China-domestic providers — all use the "deepseek" protocol
    "deepseek": "deepseek",
    "mimo": "deepseek",
    "mimo-token-plan": "deepseek",
    "qwen": "deepseek",
    "zhipu": "deepseek",
    "kimi": "deepseek",
    "doubao": "deepseek",
    "typex": "deepseek",
    "minimax": "deepseek",
}

_DEFAULT_BASE_URLS: dict[tuple[str, str], str] = {
    ("anthropic", "anthropic"): "https://api.anthropic.com",
    ("openai", "openai"): "https://api.openai.com/v1",
    ("openrouter", "openai"): "https://openrouter.ai/api/v1",
    ("deepseek", "deepseek"): "https://api.deepseek.com/v1",
    ("mimo", "deepseek"): "https://api.xiaomimimo.com/v1",
    ("mimo-token-plan", "deepseek"): "https://token-plan-cn.xiaomimimo.com/v1",
    ("qwen", "deepseek"): "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ("qwen", "anthropic"): "https://dashscope.aliyuncs.com/apps/anthropic",
    ("zhipu", "deepseek"): "https://open.bigmodel.cn/api/paas/v4",
    ("zhipu", "anthropic"): "https://open.bigmodel.cn/api/anthropic",
    ("kimi", "deepseek"): "https://api.moonshot.cn/v1",
    ("doubao", "deepseek"): "https://ark.cn-beijing.volces.com/api/v3",
    ("typex", "deepseek"): "https://newapi.typex-test.cn/v1",
    ("minimax", "deepseek"): "https://api.minimax.io/v1",
}


def resolve_protocol(config: ModelConfig) -> str:
    if config.protocol:
        return config.protocol
    return _PROVIDER_PROTOCOLS.get(config.provider, "openai")


# ── shared reasoning constants ────────────────────────────────────────────

_ANTHROPIC_BUDGETS = {
    "low": 1_024,
    "medium": 4_096,
    "high": 8_192,
}

_QWEN_THINKING_MODELS = (
    "qwen3",
    "qwq",
)

_ZHIPU_THINKING_MODELS = (
    "glm-4.5",
    "glm-4.6",
    "glm-4.7",
    "glm-5",
)


# ── DeepSeekChatOpenAI — China-domestic OpenAI-compatible subclass ────────


class DeepSeekChatOpenAI(ChatOpenAI):
    """Unified ``ChatOpenAI`` subclass for China-domestic providers.

    Solves two problems common to all these providers:

    1. **Streaming reasoning_content loss** — LangChain's
       ``_convert_delta_to_message_chunk`` silently drops ``reasoning_content``
       from the streaming delta.  We intercept the raw chunk and inject it
       into ``additional_kwargs`` so that ``_extract_thinking_openai`` can
       find it.

    2. **Provider-specific reasoning parameters** — each provider has its own
       ``extra_body`` schema for enabling/disabling thinking and mapping
       effort levels.  :meth:`reasoning_kwargs` handles the differences,
       accepting unified effort values (xhigh / high / medium / low / none)
       and mapping them to provider-specific formats internally.
    """

    # ── streaming reasoning_content preservation ──────────────────────────

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

        # Extract reasoning_content from the raw delta dict
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            rc = delta.get("reasoning_content")
            if isinstance(rc, str) and rc:
                msg.additional_kwargs["reasoning_content"] = (
                    msg.additional_kwargs.get("reasoning_content", "") + rc
                )
            # MiniMax uses reasoning_details (list of {type, text}).
            # Unlike reasoning_content (concatenated string), we preserve
            # the original item structure for downstream extraction.
            rd = delta.get("reasoning_details")
            if isinstance(rd, list) and rd:
                for item in rd:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str) and text:
                            msg.additional_kwargs.setdefault("reasoning_details", [])
                            msg.additional_kwargs["reasoning_details"].append(item)

        return generation_chunk

    # ── provider-specific reasoning effort mapping ────────────────────────

    @staticmethod
    def reasoning_kwargs(config: ModelConfig) -> dict:
        """Return reasoning kwargs for China-domestic providers.

        Accepts unified effort values (xhigh / high / medium / low / none)
        and maps them to each provider's specific format:

          - deepseek: ``reasoning_effort`` top-level + ``extra_body.thinking.type``
          - qwen:     ``extra_body.enable_thinking`` + ``thinking_budget``
          - zhipu/typex: ``extra_body.thinking.type`` (model-gated)
          - doubao:   ``extra_body.thinking.type`` (model-gated, supports "auto")
          - mimo/kimi: ``extra_body.thinking.type``
          - minimax:  ``extra_body.thinking.type`` + ``reasoning_split``

        Unknown providers with the deepseek protocol fall back to DeepSeek format.
        """
        effort = _normalized_effort(config.reasoning_effort)
        provider = config.provider

        # ── DeepSeek ──────────────────────────────────────────────────────
        if provider == "deepseek":
            if effort is None:
                return {}
            if effort == "none":
                return {"extra_body": {"thinking": {"type": "disabled"}}}
            ds_effort = "max" if effort in ("xhigh", "max") else "high"
            return {"reasoning_effort": ds_effort, "extra_body": {"thinking": {"type": "enabled"}}}

        # ── Qwen ──────────────────────────────────────────────────────────
        if provider == "qwen":
            if effort is None:
                return {}
            if not _supports_qwen_thinking(config.model):
                return {}
            if effort == "none":
                return {"extra_body": {"enable_thinking": False}}
            budget = _ANTHROPIC_BUDGETS.get(effort, _ANTHROPIC_BUDGETS["high"])
            budget = min(budget, max(config.max_tokens - 1, 1))
            return {"extra_body": {"enable_thinking": True, "thinking_budget": budget}}

        # ── Zhipu / Typex ─────────────────────────────────────────────────
        if provider in ("zhipu", "typex"):
            if effort is None:
                return {}
            if not _supports_zhipu_thinking(config.model):
                return {}
            if effort == "none":
                return {"extra_body": {"thinking": {"type": "disabled"}}}
            return {"extra_body": {"thinking": {"type": "enabled"}}}

        # ── Doubao ────────────────────────────────────────────────────────
        if provider == "doubao":
            if "thinking" not in config.model.lower() and "seed-1.6" not in config.model.lower():
                return {}
            raw_effort = (config.reasoning_effort or "").strip().lower()
            if effort is None or effort == "none":
                thinking_type = "disabled"
            elif raw_effort == "auto":
                thinking_type = "auto"
            else:
                thinking_type = "enabled"
            return {"extra_body": {"thinking": {"type": thinking_type}}}

        # ── Mimo / Kimi ───────────────────────────────────────────────────
        if provider in ("mimo", "mimo-token-plan", "kimi"):
            if effort is None:
                return {}
            if effort == "none":
                return {"extra_body": {"thinking": {"type": "disabled"}}}
            return {"extra_body": {"thinking": {"type": "enabled"}}}

        # ── MiniMax ────────────────────────────────────────────────────────
        if provider == "minimax":
            if effort is None:
                return {}
            if effort == "none":
                return {"extra_body": {"thinking": {"type": "disabled"}}}
            return {"extra_body": {"thinking": {"type": "enabled"}, "reasoning_split": True}}

        # ── Fallback: unknown provider with deepseek protocol ─────────────
        if effort is None:
            return {}
        if effort == "none":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        ds_effort = "max" if effort in ("xhigh", "max") else "high"
        return {"reasoning_effort": ds_effort, "extra_body": {"thinking": {"type": "enabled"}}}


# ── effort helpers (shared) ──────────────────────────────────────────────


def _normalized_effort(effort: str | None) -> str | None:
    if effort is None:
        return None
    value = effort.strip().lower()
    if value in {"", "off", "none"}:
        return "none"
    if value in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        return value
    return "medium"


def _supports_qwen_thinking(model: str) -> bool:
    name = model.lower()
    return name.startswith(_QWEN_THINKING_MODELS)


def _supports_zhipu_thinking(model: str) -> bool:
    name = model.lower()
    return any(p in name for p in _ZHIPU_THINKING_MODELS)


# ── Anthropic reasoning ──────────────────────────────────────────────────

_REASONING_PREFIXES = (
    "gpt-5",
    "o1",
    "o3",
    "o4",
)


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


# ── OpenAI reasoning ─────────────────────────────────────────────────────


def _openai_reasoning_effort(effort: str | None) -> str | None:
    """Map unified reasoning_effort to an effort string for the nested reasoning format."""
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
        if effort == "none" and not config.model.lower().startswith("gpt-5"):
            effort = "low"
        return {"extra_body": {"reasoning": {"effort": effort}}}

    if config.provider == "openrouter":
        if effort is None:
            return {}
        return {"extra_body": {"reasoning": {"effort": effort}}}

    # Custom providers with openai protocol: inject reasoning for known reasoning models
    if _supports_openai_reasoning(config.model):
        if effort is None:
            return {}
        if effort == "none" and not config.model.lower().startswith("gpt-5"):
            effort = "low"
        return {"extra_body": {"reasoning": {"effort": effort}}}

    return {}


# ── model factory ────────────────────────────────────────────────────────


def _reasoning_kwargs(config: ModelConfig, protocol: str) -> dict:
    if protocol == "anthropic":
        if config.provider == "anthropic":
            return _anthropic_reasoning_kwargs(config)
        return {}
    if protocol == "openai":
        return _openai_reasoning_kwargs(config)
    if protocol == "deepseek":
        return DeepSeekChatOpenAI.reasoning_kwargs(config)
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

    if protocol == "deepseek":
        kwargs = dict(
            api_key=api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        if base_url:
            kwargs["base_url"] = base_url
        kwargs.update(_reasoning_kwargs(config, protocol))
        return DeepSeekChatOpenAI(**kwargs)

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
    # Both "openai" and "deepseek" protocols use the OpenAI-compatible
    # extraction path (reasoning_content in additional_kwargs).
    return _extract_thinking_openai(chunk)


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
        "minimax": 1_000_000,
    }
    if provider in limits:
        return limits[provider]
    if protocol == "anthropic":
        return 200_000
    return 128_000
