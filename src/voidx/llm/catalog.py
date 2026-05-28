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
}

# ── fetcher registry ───────────────────────────────────────────────────────

_fetchers: dict[str, Callable[[], Awaitable[list[str]]]] = {}


def register_fetcher(provider: str, fetcher: Callable[[], Awaitable[list[str]]]) -> None:
    """Register a dynamic fetcher for a provider. Replaces static list."""
    _fetchers[provider] = fetcher


# ── built-in: OpenRouter public API ────────────────────────────────────────

OPENROUTER_API = "https://openrouter.ai/api/v1/models"


async def _fetch_openrouter_models() -> list[str]:
    """Fetch free models from OpenRouter's public /models endpoint (no auth needed)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(OPENROUTER_API)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return STATIC_MODELS.get("openrouter", [])

    free_models: list[str] = []
    for entry in data.get("data", []):
        model_id = entry.get("id", "")
        if not model_id:
            continue
        # Free models: pricing is "0" for both prompt and completion, or :free suffix
        pricing = entry.get("pricing", {})
        prompt_price = pricing.get("prompt", "-1")
        completion_price = pricing.get("completion", "-1")
        is_free = (
            model_id.endswith(":free")
            or (prompt_price == "0" and completion_price == "0")
        )
        if is_free:
            free_models.append(model_id)

    return free_models


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
