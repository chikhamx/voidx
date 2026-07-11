"""Model catalog — typed abstraction over provider model discovery.

Each provider registers a fetcher (async callable returning model names).
Providers without fetchers fall back to STATIC_MODELS.

Public interface:
    async def list_models(provider: str) -> list[str]

Registering a custom fetcher:
    register_fetcher("my_provider", my_async_fetcher)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from voidx.llm.provider import _DEFAULT_BASE_URLS, _PROVIDER_PROTOCOLS, PROTOCOL_DEEPSEEK
from voidx.logging.tool_log import log_tool_event


# ── static fallbacks ───────────────────────────────────────────────────────

STATIC_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5",
    ],
    "openai": [
        "gpt-5.5",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "o3",
        "o4-mini",
    ],
    "deepseek": [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ],
    "mimo": [
        "mimo-v2.5-pro",
        "mimo-v2.5",
        "mimo-v2.5-tts",
    ],
    "mimo-token-plan": [
        "mimo-v2.5-pro",
        "mimo-v2.5",
        "mimo-v2.5-tts",
    ],
    "qwen": [
        "qwen3.7-max",
        "qwen3-max",
        "qwen3.6-plus",
        "qwen-plus",
        "qwen-turbo",
    ],
    "zhipu": [
        "glm-5.1",
        "glm-5",
        "glm-4.7",
        "glm-4.7-flash",
    ],
    "kimi": [
        "kimi-k2.6",
        "kimi-k2.5",
        "kimi-k2",
    ],
    "doubao": [
        "doubao-seed-1.6-thinking",
        "doubao-seed-1.6",
        "doubao-seed-1.6-flash",
    ],
    "typex": [
        "zai-org/GLM-5-FP8",
    ],
    "minimax": [
        "MiniMax-M3",
        "MiniMax-M2.7",
        "MiniMax-M2.7-highspeed",
        "MiniMax-M2.5",
        "MiniMax-M2.5-highspeed",
    ],
    "longcat": [
        "LongCat-2.0",
    ],
    "xunfei-coding-plan": [
        "astron-code-latest",
    ],
    "gemini": [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ],
}

# ── fetcher registry ───────────────────────────────────────────────────────

_fetchers: dict[str, Callable[[], Awaitable[list[str]]]] = {}


def register_fetcher(provider: str, fetcher: Callable[[], Awaitable[list[str]]]) -> None:
    """Register a dynamic fetcher for a provider. Replaces static list."""
    _fetchers[provider] = fetcher


# ── built-in: OpenRouter public API ────────────────────────────────────────

OPENROUTER_API = "https://openrouter.ai/api/v1/models"


async def _fetch_openrouter_models() -> list[str]:
    """Fetch models from OpenRouter's public /models endpoint. Free models first,
    filtered to chat/language models only, limited to a manageable count."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(OPENROUTER_API)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return STATIC_MODELS.get("openrouter", [])

    _skip_keywords = (
        "embed", "moderation", "image", "vision", "audio",
        "whisper", "tts", "dall-e", "dalle", "transcribe",
    )

    free_models: list[str] = []
    paid_models: list[str] = []
    seen: set[str] = set()

    for entry in data.get("data", []):
        model_id = entry.get("id", "")
        if not model_id:
            continue
        # Skip empty/invalid models
        ctx_len = entry.get("context_length", 0) or 0
        if ctx_len <= 0:
            continue
        # Skip obviously non-chat models
        mid_lower = model_id.lower()
        if any(kw in mid_lower for kw in _skip_keywords):
            continue
        # Dedup: if both x and x:free exist, prefer :free for free list
        base = model_id.removesuffix(":free")
        pricing = entry.get("pricing", {})
        prompt_price = pricing.get("prompt", "-1")
        completion_price = pricing.get("completion", "-1")
        is_free = (
            model_id.endswith(":free")
            or (prompt_price == "0" and completion_price == "0")
        )
        if is_free:
            if base not in seen:
                seen.add(base)
                free_models.append(model_id)
        else:
            if base not in seen:
                seen.add(base)
                paid_models.append(model_id)

    result = free_models + paid_models
    # Limit total to keep the selector usable
    return result[:100]


register_fetcher("openrouter", _fetch_openrouter_models)

# ── settings binding (needed by fetchers) ─────────────────────────────────

_settings = None

def bind_settings(settings) -> None:
    """Bind a Settings instance so list_models() can merge custom models."""
    global _settings
    _settings = settings


# ── built-in: OpenAI-compatible providers ──────────────────────────────────

_SKIP_KEYWORDS = (
    "embed", "moderation", "image", "vision", "audio",
    "whisper", "tts", "dall-e", "dalle", "transcribe",
)

_OPENAI_COMPATIBLE_PROVIDERS = [
    "openai", "deepseek", "mimo", "mimo-token-plan",
    "qwen", "zhipu", "kimi", "doubao",
    "typex", "minimax", "longcat", "xunfei-coding-plan",
]


async def _resolve_base_url(provider: str) -> str:
    """Resolve base URL: user-configured (from settings) or built-in default."""
    if _settings is not None:
        try:
            url = await _settings.resolve_base_url(provider)
            if url:
                return url.rstrip("/")
        except Exception as exc:
            log_tool_event("llm_resolve_base_url", tool_name="catalog", message=str(exc))
    protocol = _PROVIDER_PROTOCOLS.get(provider, "openai")
    default = _DEFAULT_BASE_URLS.get((provider, protocol), "")
    return default.rstrip("/")


async def _resolve_api_key(provider: str) -> str | None:
    """Resolve API key from settings, if available."""
    if _settings is None:
        return None
    try:
        return await _settings.resolve_api_key(provider)
    except Exception as exc:
        log_tool_event("llm_resolve_api_key", tool_name="catalog", message=str(exc))
        return None


async def _fetch_openai_compatible_models(provider: str) -> list[str]:
    """Fetch models from an OpenAI-compatible /models endpoint.

    Used by 12 providers: openai, deepseek, mimo, mimo-token-plan, qwen,
    zhipu, kimi, doubao, typex, minimax, longcat, xunfei-coding-plan.
    Falls back to STATIC_MODELS on any error or missing API key.
    """
    api_key = await _resolve_api_key(provider)
    if not api_key:
        return STATIC_MODELS.get(provider, [])

    base_url = await _resolve_base_url(provider)
    if not base_url:
        return STATIC_MODELS.get(provider, [])

    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log_tool_event("catalog_fetch_failed", tool_name="catalog",
                           message=f"Failed to fetch models for {provider}: {exc}")
            return STATIC_MODELS.get(provider, [])

    models: list[str] = []
    seen: set[str] = set()
    for entry in data.get("data", []):
        model_id = entry.get("id", "")
        if not model_id:
            continue
        mid_lower = model_id.lower()
        if any(kw in mid_lower for kw in _SKIP_KEYWORDS):
            continue
        if model_id not in seen:
            seen.add(model_id)
            models.append(model_id)

    if not models:
        return STATIC_MODELS.get(provider, [])
    return models[:100]


# ── built-in: Anthropic ────────────────────────────────────────────────────

_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


async def _fetch_anthropic_models() -> list[str]:
    """Fetch models from Anthropic's /v1/models endpoint."""
    api_key = await _resolve_api_key("anthropic")
    if not api_key:
        return STATIC_MODELS.get("anthropic", [])

    base_url = await _resolve_base_url("anthropic") or _ANTHROPIC_BASE_URL
    url = f"{base_url}/v1/models"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log_tool_event("catalog_fetch_failed", tool_name="catalog",
                           message=f"Failed to fetch models for anthropic: {exc}")
            return STATIC_MODELS.get("anthropic", [])

    models: list[str] = []
    seen: set[str] = set()
    for entry in data.get("data", []):
        model_id = entry.get("id", "")
        if not model_id:
            continue
        if model_id not in seen:
            seen.add(model_id)
            models.append(model_id)

    if not models:
        return STATIC_MODELS.get("anthropic", [])
    return models


# ── built-in: Gemini ───────────────────────────────────────────────────────

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
_GEMINI_SKIP_KEYWORDS = ("embed", "aqa")


async def _fetch_gemini_models() -> list[str]:
    """Fetch models from Gemini's /v1beta/models endpoint.

    Gemini returns models[].name as 'models/gemini-xxx'; we strip the prefix.
    Filters out embedding and non-generative models.
    """
    api_key = await _resolve_api_key("gemini")
    if not api_key:
        return STATIC_MODELS.get("gemini", [])

    base_url = await _resolve_base_url("gemini") or _GEMINI_BASE_URL
    url = f"{base_url}/v1beta/models"
    params = {"key": api_key}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log_tool_event("catalog_fetch_failed", tool_name="catalog",
                           message=f"Failed to fetch models for gemini: {exc}")
            return STATIC_MODELS.get("gemini", [])

    models: list[str] = []
    seen: set[str] = set()
    for entry in data.get("models", []):
        name = entry.get("name", "")
        if not name:
            continue
        model_id = name.removeprefix("models/")
        mid_lower = model_id.lower()
        if any(kw in mid_lower for kw in _GEMINI_SKIP_KEYWORDS):
            continue
        if model_id not in seen:
            seen.add(model_id)
            models.append(model_id)

    if not models:
        return STATIC_MODELS.get("gemini", [])
    return models


# ── register all built-in fetchers ────────────────────────────────────────

for _provider in _OPENAI_COMPATIBLE_PROVIDERS:
    register_fetcher(_provider, lambda p=_provider: _fetch_openai_compatible_models(p))
    register_fetcher("anthropic", _fetch_anthropic_models)
    register_fetcher("gemini", _fetch_gemini_models)


async def _merge_custom(provider: str, base: list[str]) -> list[str]:
    """Merge custom models (from settings) in front of base list, deduplicating."""
    if _settings is None:
        return base
    custom = await _settings.list_custom_models(provider)
    if not custom:
        return base
    seen: set[str] = set()
    result: list[str] = []
    for m in custom + base:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


# ── public API ─────────────────────────────────────────────────────────────

async def list_models(provider: str) -> list[str]:
    """Return available model names for a provider.

    If a dynamic fetcher is registered, it's called first. On failure or if
    no fetcher exists, falls back to STATIC_MODELS.
    Custom models from settings are merged in front of the result.
    """
    fetcher = _fetchers.get(provider)
    if fetcher is not None:
        try:
            models = await fetcher()
            if models:
                return await _merge_custom(provider, models)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
            log_tool_event("catalog_fetch_failed", tool_name="catalog", message=f"Failed to fetch models for {provider}: {exc}")
        except Exception as exc:
            log_tool_event("catalog_fetch_unexpected", tool_name="catalog", message=f"Unexpected error fetching models for {provider}: {exc}")
    return await _merge_custom(provider, STATIC_MODELS.get(provider, []))
