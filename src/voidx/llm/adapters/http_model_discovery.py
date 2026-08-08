"""HTTP adapter for provider model discovery."""

from __future__ import annotations

from collections.abc import Callable

import asyncio

import httpx

from voidx.llm.application.model_catalog import sort_models_latest_first
from voidx.llm.providers.gemini import strip_gemini_version_suffix
from voidx.observability.tool_log import log_tool_event


def catalog_log_event(event: str, **kwargs) -> None:
    log_tool_event(event, **kwargs)

OPENROUTER_API = "https://openrouter.ai/api/v1/models"
_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
_SKIP_KEYWORDS = (
    "embed",
    "moderation",
    "image",
    "vision",
    "audio",
    "whisper",
    "tts",
    "dall-e",
    "dalle",
    "transcribe",
)
_GEMINI_SKIP_KEYWORDS = ("embed", "aqa")


class HttpModelDiscovery:
    """Discover provider models through their public HTTP APIs."""

    def __init__(self, log_event: Callable[..., None] | None = None) -> None:
        self._log_event = log_event or (lambda *args, **kwargs: None)

    async def fetch_models(
        self,
        provider: str,
        *,
        protocol: str,
        api_key: str | None,
        base_url: str | None,
    ) -> list[str]:
        if provider == "openrouter":
            return await self._fetch_openrouter()
        if protocol == "anthropic":
            return await self._fetch_anthropic(provider, api_key, base_url)
        if protocol == "gemini":
            return await self._fetch_gemini(provider, api_key, base_url)
        return await self._fetch_openai_compatible(provider, api_key, base_url)

    async def _get(
        self,
        provider: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
                self._log_event(
                    "catalog_fetch_failed",
                    tool_name="catalog",
                    message=f"Failed to fetch models for {provider}: {exc}",
                )
                return {}
            except Exception as exc:
                self._log_event(
                    "catalog_fetch_unexpected",
                    tool_name="catalog",
                    message=f"Unexpected error fetching models for {provider}: {exc}",
                )
                return {}

    async def _fetch_openrouter(self) -> list[str]:
        data = await self._get("openrouter", OPENROUTER_API)
        free_models: list[str] = []
        paid_models: list[str] = []
        seen: set[str] = set()
        for entry in data.get("data", []):
            model_id = entry.get("id", "")
            if not model_id or (entry.get("context_length", 0) or 0) <= 0:
                continue
            if any(keyword in model_id.lower() for keyword in _SKIP_KEYWORDS):
                continue
            base = model_id.removesuffix(":free")
            pricing = entry.get("pricing", {})
            is_free = model_id.endswith(":free") or (
                pricing.get("prompt", "-1") == "0"
                and pricing.get("completion", "-1") == "0"
            )
            if base in seen:
                continue
            seen.add(base)
            (free_models if is_free else paid_models).append(model_id)
        return (free_models + paid_models)[:100]

    async def _fetch_openai_compatible(
        self,
        provider: str,
        api_key: str | None,
        base_url: str | None,
    ) -> list[str]:
        if not api_key or not base_url:
            return []
        data = await self._get(
            provider,
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        models: list[str] = []
        seen: set[str] = set()
        for entry in data.get("data", []):
            model_id = entry.get("id", "")
            if not model_id or any(
                keyword in model_id.lower() for keyword in _SKIP_KEYWORDS
            ):
                continue
            if model_id not in seen:
                seen.add(model_id)
                models.append(model_id)
        return sort_models_latest_first(models)[:100]

    async def _fetch_anthropic(
        self,
        provider: str,
        api_key: str | None,
        base_url: str | None,
    ) -> list[str]:
        if not api_key:
            return []
        root = (base_url or _ANTHROPIC_BASE_URL).rstrip("/")
        data = await self._get(
            provider,
            f"{root}/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
            },
        )
        models = list(
            dict.fromkeys(
                entry.get("id", "")
                for entry in data.get("data", [])
                if entry.get("id", "")
            )
        )
        return sort_models_latest_first(models)

    async def _fetch_gemini(
        self,
        provider: str,
        api_key: str | None,
        base_url: str | None,
    ) -> list[str]:
        if not api_key:
            return []
        root = strip_gemini_version_suffix(base_url or _GEMINI_BASE_URL)
        data = await self._get(
            provider,
            f"{root}/v1beta/models",
            headers={"x-goog-api-key": api_key},
            params={"key": api_key},
        )
        models: list[str] = []
        seen: set[str] = set()
        for entry in data.get("models", []):
            model_id = entry.get("name", "").removeprefix("models/")
            if not model_id or any(
                keyword in model_id.lower() for keyword in _GEMINI_SKIP_KEYWORDS
            ):
                continue
            if model_id not in seen:
                seen.add(model_id)
                models.append(model_id)
        return sort_models_latest_first(models)


__all__ = ["HttpModelDiscovery"]
