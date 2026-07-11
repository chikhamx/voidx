import httpx
import pytest

from voidx.llm import catalog


@pytest.mark.asyncio
async def test_list_models_expected_fetcher_failure_falls_back(monkeypatch):
    provider = "expected-failure-provider"

    async def failing_fetcher():
        raise httpx.HTTPError("upstream failed")

    monkeypatch.setattr(catalog, "_settings", None)
    monkeypatch.setitem(catalog.STATIC_MODELS, provider, ["static-model"])
    monkeypatch.setitem(catalog._fetchers, provider, failing_fetcher)

    models = await catalog.list_models(provider)

    assert models == ["static-model"]


@pytest.mark.asyncio
async def test_list_models_unexpected_fetcher_failure_falls_back(monkeypatch):
    provider = "unexpected-failure-provider"

    async def failing_fetcher():
        raise RuntimeError("bug")

    monkeypatch.setattr(catalog, "_settings", None)
    monkeypatch.setitem(catalog.STATIC_MODELS, provider, ["static-model"])
    monkeypatch.setitem(catalog._fetchers, provider, failing_fetcher)

    models = await catalog.list_models(provider)

    assert models == ["static-model"]


def test_xunfei_coding_plan_static_models():
    assert "astron-code-latest" in catalog.STATIC_MODELS.get("xunfei-coding-plan", [])
