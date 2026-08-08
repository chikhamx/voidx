from __future__ import annotations

import pytest

from voidx.agent.slash import runtime
from voidx.llm.domain.model import ModelConfig
from voidx.llm import catalog, provider, service
from voidx.llm.providers import base
from voidx.llm.providers.base import ProviderSpec


def test_registered_reasoning_hook_wins_for_every_protocol(monkeypatch):
    calls: list[str] = []

    def reasoning(config: ModelConfig) -> dict:
        calls.append(config.provider)
        return {"marker": config.provider}

    for protocol in ("anthropic", "gemini", "deepseek", "openai"):
        name = f"runtime-{protocol}"
        monkeypatch.setitem(
            base._SPECS,
            name,
            ProviderSpec(name=name, protocol=protocol, reasoning=reasoning),
        )
        config = ModelConfig(
            provider=name,
            protocol=protocol,
            model="test-model",
            reasoning_effort="high",
        )

        assert provider._reasoning_kwargs(config, protocol) == {"marker": name}

    assert calls == [
        "runtime-anthropic",
        "runtime-gemini",
        "runtime-deepseek",
        "runtime-openai",
    ]


def test_registered_provider_without_reasoning_does_not_use_protocol_default(monkeypatch):
    name = "runtime-no-reasoning"
    monkeypatch.setitem(
        base._SPECS,
        name,
        ProviderSpec(name=name, protocol="deepseek"),
    )
    config = ModelConfig(
        provider=name,
        protocol="deepseek",
        model="test-model",
        reasoning_effort="high",
    )

    assert provider._reasoning_kwargs(config, "deepseek") == {}


@pytest.mark.asyncio
async def test_catalog_sees_provider_registered_after_import(monkeypatch):
    name = "runtime-catalog-provider"
    monkeypatch.setitem(
        base._SPECS,
        name,
        ProviderSpec(
            name=name,
            protocol="openai",
            static_models=("runtime-model",),
        ),
    )
    monkeypatch.setattr(catalog, "_settings", None)

    assert await catalog.list_models(name) == ["runtime-model"]


@pytest.mark.asyncio
async def test_catalog_registered_fetcher_overrides_runtime_provider(monkeypatch):
    name = "runtime-fetcher-provider"
    monkeypatch.setitem(
        base._SPECS,
        name,
        ProviderSpec(name=name, protocol="openai", static_models=("static",)),
    )

    async def fetcher() -> list[str]:
        return ["dynamic"]

    monkeypatch.setitem(catalog._fetchers, name, fetcher)

    assert await catalog.list_models(name) == ["dynamic"]


@pytest.mark.asyncio
async def test_slash_builtin_providers_follow_registry_order(monkeypatch):
    name = "runtime-slash-provider"
    monkeypatch.setitem(
        base._SPECS,
        name,
        ProviderSpec(name=name, protocol="openai"),
    )

    providers = await runtime.get_providers()

    assert providers == [spec.name for spec in base.all_specs()]
    assert runtime.PROVIDERS == [spec.name for spec in base.all_specs()]


def test_service_exposes_resolver_structured_output_capability():
    class CapabilityModel:
        resolver_structured_output_method = "json_mode"

    assert service.get_resolver_structured_output_method(CapabilityModel()) == "json_mode"
    assert service.get_resolver_structured_output_method(object()) is None
