"""Model catalog — typed abstraction over provider model discovery.

Each provider registers a fetcher (async callable returning model names).
Providers without fetchers fall back to STATIC_MODELS.

Public interface:
    async def list_models(provider: str) -> list[str]

Registering a custom fetcher:
    register_fetcher("my_provider", my_async_fetcher)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

# ── static fallbacks ───────────────────────────────────────────────────────

STATIC_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4.1",
        "gpt-4.1-mini",
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

# ── public API ─────────────────────────────────────────────────────────────

async def list_models(provider: str) -> list[str]:
    """Return available model names for a provider.

    If a dynamic fetcher is registered, it's called first. On failure or if
    no fetcher exists, falls back to STATIC_MODELS.
    """
    fetcher = _fetchers.get(provider)
    if fetcher is not None:
        try:
            models = await fetcher()
            if models:
                return models
        except Exception:
            pass
    return STATIC_MODELS.get(provider, [])
