from __future__ import annotations

import pytest

from voidx.llm.application.model_catalog import ModelCatalog
from voidx.llm.providers.base import ProviderSpec


class FakeSettings:
    def __init__(self, custom_model: str):
        self.custom_model = custom_model

    async def resolve_api_key(self, provider: str) -> str | None:
        return None

    async def resolve_base_url(self, provider: str) -> str | None:
        return None

    async def list_custom_models(self, provider: str) -> list[str]:
        return [self.custom_model]


class FakeDiscovery:
    def __init__(self, model: str):
        self.model = model

    async def fetch_models(self, provider: str, **kwargs) -> list[str]:
        return [self.model]


@pytest.mark.asyncio
async def test_catalog_instances_do_not_leak_settings_or_discovery() -> None:
    spec = ProviderSpec(
        name="demo",
        protocol="openai",
        static_models=("static",),
    )
    first = ModelCatalog(
        provider_specs=(spec,),
        settings=FakeSettings("custom-first"),
        discovery=FakeDiscovery("dynamic-first"),
    )
    second = ModelCatalog(
        provider_specs=(spec,),
        settings=FakeSettings("custom-second"),
        discovery=FakeDiscovery("dynamic-second"),
    )

    assert await first.list_models("demo") == ["custom-first", "dynamic-first"]
    assert await second.list_models("demo") == ["custom-second", "dynamic-second"]
    assert await first.list_models("demo") == ["custom-first", "dynamic-first"]


@pytest.mark.asyncio
async def test_catalog_fetcher_overrides_are_instance_scoped() -> None:
    spec = ProviderSpec(name="demo", protocol="openai", static_models=("static",))

    async def override() -> list[str]:
        return ["override"]

    overridden = ModelCatalog(
        provider_specs=(spec,),
        fetchers={"demo": override},
    )
    plain = ModelCatalog(provider_specs=(spec,))

    assert await overridden.list_models("demo") == ["override"]
    assert await plain.list_models("demo") == ["static"]


@pytest.mark.asyncio
async def test_settings_resolution_failures_are_logged_without_leaking() -> None:
    class FailingSettings:
        async def resolve_api_key(self, provider: str) -> str | None:
            raise RuntimeError("key failed")

        async def resolve_base_url(self, provider: str) -> str | None:
            raise RuntimeError("url failed")

        async def list_custom_models(self, provider: str) -> list[str]:
            return []

    events: list[tuple[str, str]] = []
    catalog = ModelCatalog(
        provider_specs=(ProviderSpec(name="demo", protocol="openai"),),
        settings=FailingSettings(),
        discovery=FakeDiscovery("dynamic"),
        log_event=lambda event, **kwargs: events.append((event, kwargs["message"])),
    )

    assert await catalog.list_models("demo") == ["dynamic"]
    assert events == [
        ("llm_resolve_api_key", "key failed"),
        ("llm_resolve_base_url", "url failed"),
    ]
