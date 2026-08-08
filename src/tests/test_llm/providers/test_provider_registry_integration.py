from __future__ import annotations

import pytest

from voidx.agent.slash import runtime
from voidx.llm import catalog, provider, service
from voidx.llm.domain.model import ModelConfig
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.catalog import PROVIDER_SPECS


def test_explicit_reasoning_hook_wins_for_every_protocol(monkeypatch):
    calls: list[str] = []

    def reasoning(config: ModelConfig) -> dict:
        calls.append(config.provider)
        return {"marker": config.provider}

    specs = tuple(
        ProviderSpec(
            name=f"runtime-{protocol}",
            protocol=protocol,
            reasoning=reasoning,
        )
        for protocol in ("anthropic", "gemini", "deepseek", "openai")
    )
    monkeypatch.setattr(provider, "PROVIDER_SPECS", specs)

    for spec in specs:
        config = ModelConfig(
            provider=spec.name,
            protocol=spec.protocol,
            model="test-model",
            reasoning_effort="high",
        )
        assert provider._reasoning_kwargs(config, spec.protocol) == {
            "marker": spec.name
        }

    assert calls == [spec.name for spec in specs]


def test_explicit_provider_without_reasoning_does_not_use_protocol_default(monkeypatch):
    spec = ProviderSpec(name="runtime-no-reasoning", protocol="deepseek")
    monkeypatch.setattr(provider, "PROVIDER_SPECS", (spec,))
    config = ModelConfig(
        provider=spec.name,
        protocol=spec.protocol,
        model="test-model",
        reasoning_effort="high",
    )

    assert provider._reasoning_kwargs(config, "deepseek") == {}


@pytest.mark.asyncio
async def test_catalog_uses_explicit_optional_provider_specs(monkeypatch):
    spec = ProviderSpec(
        name="runtime-catalog-provider",
        protocol="openai",
        static_models=("runtime-model",),
    )
    monkeypatch.setattr(catalog, "_settings", None)

    assert await catalog.list_models(spec.name, provider_specs=(spec,)) == [
        "runtime-model"
    ]


@pytest.mark.asyncio
async def test_catalog_registered_fetcher_overrides_explicit_provider(monkeypatch):
    spec = ProviderSpec(
        name="runtime-fetcher-provider",
        protocol="openai",
        static_models=("static",),
    )

    async def fetcher() -> list[str]:
        return ["dynamic"]

    monkeypatch.setitem(catalog._fetchers, spec.name, fetcher)

    assert await catalog.list_models(spec.name, provider_specs=(spec,)) == ["dynamic"]


@pytest.mark.asyncio
async def test_catalog_same_name_override_controls_fetcher_fallback(monkeypatch):
    override = ProviderSpec(
        name="deepseek",
        protocol="openai",
        static_models=("override-model",),
    )
    monkeypatch.setattr(catalog, "_settings", None)

    assert await catalog.list_models(
        "deepseek",
        provider_specs=(override,),
    ) == ["override-model"]


@pytest.mark.asyncio
async def test_catalog_same_name_override_controls_default_base_url(monkeypatch):
    override = ProviderSpec(
        name="deepseek",
        protocol="openai",
        default_base_url="https://override.example/v1",
        static_models=("override-model",),
    )

    class Settings:
        async def resolve_api_key(self, provider: str) -> str:
            return "override-key"

        async def resolve_base_url(self, provider: str) -> None:
            return None

        async def list_custom_models(self, provider: str) -> list[str]:
            return []

    requested_urls: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": []}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            requested_urls.append(url)
            return Response()

    monkeypatch.setattr(catalog, "_settings", Settings())
    monkeypatch.setattr(catalog.httpx, "AsyncClient", Client)

    assert await catalog.list_models(
        "deepseek",
        provider_specs=(override,),
    ) == ["override-model"]
    assert requested_urls == ["https://override.example/v1/models"]


@pytest.mark.asyncio
async def test_slash_builtin_providers_follow_explicit_catalog_order():
    specs = (
        ProviderSpec(name="runtime-first", protocol="openai"),
        ProviderSpec(name="runtime-second", protocol="openai"),
    )

    providers = await runtime.get_providers(provider_specs=specs)

    assert providers == ["runtime-first", "runtime-second"]
    assert runtime._builtin_providers(specs) == ["runtime-first", "runtime-second"]


def test_builtin_catalog_is_immutable_tuple():
    assert isinstance(PROVIDER_SPECS, tuple)


def test_service_exposes_resolver_structured_output_capability():
    class CapabilityModel:
        resolver_structured_output_method = "json_mode"

    assert service.get_resolver_structured_output_method(CapabilityModel()) == "json_mode"
    assert service.get_resolver_structured_output_method(object()) is None
