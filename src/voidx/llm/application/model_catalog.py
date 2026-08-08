"""Workspace-scoped model catalog use case."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping

from voidx.llm.application.ports import CatalogSettings, ModelDiscovery
from voidx.llm.providers.base import ProviderSpec

ModelFetcher = Callable[[], Awaitable[list[str]]]
_VERSION_TOKEN_RE = re.compile(r"\d+")


def sort_models_latest_first(models: list[str]) -> list[str]:
    versioned = [
        (index, model, tuple(int(part) for part in _VERSION_TOKEN_RE.findall(model)))
        for index, model in enumerate(models)
    ]
    width = max((len(version) for _index, _model, version in versioned), default=0)

    def key(item: tuple[int, str, tuple[int, ...]]) -> tuple:
        index, _model, version = item
        if not version:
            return (1, (), index)
        padded = version + (0,) * (width - len(version))
        return (0, tuple(-part for part in padded), index)

    return [model for _index, model, _version in sorted(versioned, key=key)]


class ModelCatalog:
    def __init__(
        self,
        *,
        provider_specs: tuple[ProviderSpec, ...],
        settings: CatalogSettings | None = None,
        discovery: ModelDiscovery | None = None,
        fetchers: Mapping[str, ModelFetcher] | None = None,
        log_event: Callable[..., None] | None = None,
    ) -> None:
        self._provider_specs = provider_specs
        self._settings = settings
        self._discovery = discovery
        self._fetchers = dict(fetchers or {})
        self._log_event = log_event or (lambda *args, **kwargs: None)

    def _spec(self, provider: str) -> ProviderSpec | None:
        return next(
            (spec for spec in self._provider_specs if spec.name == provider),
            None,
        )

    def _static_models(self, provider: str) -> list[str]:
        spec = self._spec(provider)
        return list(spec.static_models) if spec is not None else []

    async def _custom_models(self, provider: str) -> list[str]:
        if self._settings is None:
            return []
        return await self._settings.list_custom_models(provider)

    async def _resolve_api_key(self, provider: str) -> str | None:
        if self._settings is None:
            return None
        try:
            return await self._settings.resolve_api_key(provider)
        except Exception as exc:
            self._log_event(
                "llm_resolve_api_key",
                tool_name="catalog",
                message=str(exc),
            )
            return None

    async def _resolve_base_url(self, provider: str) -> str | None:
        if self._settings is None:
            return None
        try:
            return await self._settings.resolve_base_url(provider)
        except Exception as exc:
            self._log_event(
                "llm_resolve_base_url",
                tool_name="catalog",
                message=str(exc),
            )
            return None

    async def _merge_custom(self, provider: str, models: list[str]) -> list[str]:
        seen: set[str] = set()
        merged: list[str] = []
        for model in await self._custom_models(provider) + models:
            if model not in seen:
                seen.add(model)
                merged.append(model)
        return sort_models_latest_first(merged)

    async def list_fallback_models(
        self,
        provider: str,
        protocol: str | None = None,
    ) -> list[str]:
        models = self._static_models(provider)
        if not models and protocol:
            models = self._static_models(protocol)
        return await self._merge_custom(provider, models)

    async def list_models(self, provider: str) -> list[str]:
        fetcher = self._fetchers.get(provider)
        models: list[str] = []
        if fetcher is not None:
            try:
                models = await fetcher()
            except Exception:
                models = []
        elif self._discovery is not None:
            spec = self._spec(provider)
            protocol = spec.protocol if spec is not None else "openai"
            api_key = await self._resolve_api_key(provider)
            base_url = await self._resolve_base_url(provider)
            if not base_url and spec is not None:
                base_url = spec.default_base_url
            try:
                models = await self._discovery.fetch_models(
                    provider,
                    protocol=protocol,
                    api_key=api_key,
                    base_url=base_url,
                )
            except Exception:
                models = []
        if not models:
            models = self._static_models(provider)
        return await self._merge_custom(provider, models)

    async def list_models_for_config(
        self,
        provider: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        protocol: str | None = None,
    ) -> list[str]:
        spec = self._spec(provider)
        resolved_protocol = protocol or (spec.protocol if spec is not None else "openai")
        if not base_url and spec is not None:
            base_url = spec.default_base_url
        models: list[str] = []
        if self._discovery is not None:
            try:
                models = await self._discovery.fetch_models(
                    provider,
                    protocol=resolved_protocol,
                    api_key=api_key,
                    base_url=base_url,
                )
            except Exception:
                models = []
        if not models:
            return await self.list_fallback_models(provider, resolved_protocol)
        return await self._merge_custom(provider, models)
