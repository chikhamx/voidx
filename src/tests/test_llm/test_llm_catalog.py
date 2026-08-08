import httpx
import pytest

from voidx.llm import catalog
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.providers.catalog import PROVIDER_SPECS


@pytest.mark.asyncio
async def test_list_models_expected_fetcher_failure_falls_back(monkeypatch):
    provider = "expected-failure-provider"

    async def failing_fetcher():
        raise httpx.HTTPError("upstream failed")

    monkeypatch.setattr(catalog, "_settings", None)
    spec = ProviderSpec(name=provider, protocol="openai", static_models=("static-model",))
    monkeypatch.setitem(catalog._fetchers, provider, failing_fetcher)

    models = await catalog.list_models(provider, provider_specs=(spec,))

    assert models == ["static-model"]


@pytest.mark.asyncio
async def test_list_models_unexpected_fetcher_failure_falls_back(monkeypatch):
    provider = "unexpected-failure-provider"

    async def failing_fetcher():
        raise RuntimeError("bug")

    monkeypatch.setattr(catalog, "_settings", None)
    spec = ProviderSpec(name=provider, protocol="openai", static_models=("static-model",))
    monkeypatch.setitem(catalog._fetchers, provider, failing_fetcher)

    models = await catalog.list_models(provider, provider_specs=(spec,))

    assert models == ["static-model"]


def test_xunfei_coding_plan_static_models():
    spec = next(item for item in PROVIDER_SPECS if item.name == "xunfei-coding-plan")
    assert "astron-code-latest" in spec.static_models
