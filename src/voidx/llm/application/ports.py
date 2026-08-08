"""Ports used by LLM application services."""

from __future__ import annotations

from typing import Protocol


class CatalogSettings(Protocol):
    async def resolve_api_key(self, provider: str) -> str | None: ...

    async def resolve_base_url(self, provider: str) -> str | None: ...

    async def list_custom_models(self, provider: str) -> list[str]: ...


class ModelDiscovery(Protocol):
    async def fetch_models(
        self,
        provider: str,
        *,
        protocol: str,
        api_key: str | None,
        base_url: str | None,
    ) -> list[str]: ...
