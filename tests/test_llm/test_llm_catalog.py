import logging
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.llm import catalog


@pytest.mark.asyncio
async def test_list_models_logs_expected_fetcher_failure_and_falls_back(monkeypatch, caplog):
    provider = "expected-failure-provider"

    async def failing_fetcher():
        raise httpx.HTTPError("upstream failed")

    monkeypatch.setattr(catalog, "_settings", None)
    monkeypatch.setitem(catalog.STATIC_MODELS, provider, ["static-model"])
    monkeypatch.setitem(catalog._fetchers, provider, failing_fetcher)

    with caplog.at_level(logging.DEBUG, logger="voidx.llm.catalog"):
        models = await catalog.list_models(provider)

    assert models == ["static-model"]
    assert f"Failed to fetch models for {provider}" in caplog.text


@pytest.mark.asyncio
async def test_list_models_logs_unexpected_fetcher_failure_and_falls_back(monkeypatch, caplog):
    provider = "unexpected-failure-provider"

    async def failing_fetcher():
        raise RuntimeError("bug")

    monkeypatch.setattr(catalog, "_settings", None)
    monkeypatch.setitem(catalog.STATIC_MODELS, provider, ["static-model"])
    monkeypatch.setitem(catalog._fetchers, provider, failing_fetcher)

    with caplog.at_level(logging.DEBUG, logger="voidx.llm.catalog"):
        models = await catalog.list_models(provider)

    assert models == ["static-model"]
    assert f"Unexpected error fetching models for {provider}" in caplog.text


def test_xunfei_coding_plan_static_models():
    assert "astron-code-latest" in catalog.STATIC_MODELS.get("xunfei-coding-plan", [])
