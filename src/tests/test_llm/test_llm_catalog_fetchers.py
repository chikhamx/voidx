"""Focused tests for HTTP model discovery and catalog fallback behavior."""

from __future__ import annotations

import httpx
import pytest

from voidx.llm.adapters import http_model_discovery as adapter
from voidx.llm.adapters.http_model_discovery import HttpModelDiscovery
from voidx.llm.application.model_catalog import ModelCatalog
from voidx.llm.providers.catalog import PROVIDER_SPECS


class FakeSettings:
    def __init__(self, keys=None, base_urls=None, custom_models=None):
        self.keys = keys or {}
        self.base_urls = base_urls or {}
        self.custom_models = custom_models or {}

    async def resolve_api_key(self, provider: str) -> str | None:
        return self.keys.get(provider)

    async def resolve_base_url(self, provider: str) -> str | None:
        return self.base_urls.get(provider)

    async def list_custom_models(self, provider: str) -> list[str]:
        return self.custom_models.get(provider, [])


def make_catalog(settings: FakeSettings) -> ModelCatalog:
    return ModelCatalog(
        provider_specs=PROVIDER_SPECS,
        settings=settings,
        discovery=HttpModelDiscovery(),
    )


def mock_httpx(monkeypatch, handler):
    class Response:
        def __init__(self, status, body):
            self.status_code = status
            self.body = body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=None, response=self)

        def json(self):
            return self.body

    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("timeout") == 15.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, *, headers=None, params=None):
            status, body = handler(url, headers or {}, params or {})
            return Response(status, body)

    monkeypatch.setattr(adapter.httpx, "AsyncClient", Client)


@pytest.mark.asyncio
async def test_openai_compatible_parse_filter_sort_and_custom_base(monkeypatch):
    def handler(url, headers, params):
        assert url == "https://relay.example/v1/models"
        assert headers["Authorization"] == "Bearer sk-test"
        return 200, {"data": [
            {"id": "gpt-4o"},
            {"id": "gpt-5.4-mini"},
            {"id": "gpt-5.10-mini"},
            {"id": "text-embedding-3"},
            {"id": "gpt-5.4-mini"},
        ]}

    mock_httpx(monkeypatch, handler)
    catalog = make_catalog(FakeSettings(
        keys={"openai": "sk-test"},
        base_urls={"openai": "https://relay.example/v1/"},
    ))

    assert await catalog.list_models("openai") == [
        "gpt-5.10-mini", "gpt-5.4-mini", "gpt-4o"
    ]


@pytest.mark.asyncio
async def test_anthropic_parse_headers_and_sort(monkeypatch):
    def handler(url, headers, params):
        assert url == "https://api.anthropic.com/v1/models"
        assert headers["x-api-key"] == "sk-ant"
        assert headers["anthropic-version"] == "2023-06-01"
        return 200, {"data": [
            {"id": "claude-sonnet-4-6"}, {"id": "claude-opus-4-8"}
        ]}

    mock_httpx(monkeypatch, handler)
    models = await make_catalog(FakeSettings(keys={"anthropic": "sk-ant"})).list_models("anthropic")
    assert models == ["claude-opus-4-8", "claude-sonnet-4-6"]


@pytest.mark.asyncio
async def test_gemini_parse_filter_and_strip_version_suffix(monkeypatch):
    def handler(url, headers, params):
        assert url == "http://relay.example/antigravity/v1beta/models"
        assert params["key"] == "AIza-test"
        assert headers["x-goog-api-key"] == "AIza-test"
        return 200, {"models": [
            {"name": "models/gemini-2.5-flash"},
            {"name": "models/gemini-3.1-pro-high"},
            {"name": "models/text-embedding-004"},
        ]}

    mock_httpx(monkeypatch, handler)
    catalog = make_catalog(FakeSettings())
    models = await catalog.list_models_for_config(
        "custom",
        api_key="AIza-test",
        base_url="http://relay.example/antigravity/v1beta",
        protocol="gemini",
    )
    assert models == ["gemini-3.1-pro-high", "gemini-2.5-flash"]


@pytest.mark.asyncio
async def test_openrouter_free_first_filter_dedup_and_limit(monkeypatch):
    entries = [
        {"id": "vendor/free:free", "context_length": 100, "pricing": {}},
        {"id": "vendor/free", "context_length": 100, "pricing": {"prompt": "1"}},
        {"id": "vendor/paid", "context_length": 100, "pricing": {"prompt": "1"}},
        {"id": "vendor/embed", "context_length": 100, "pricing": {}},
        {"id": "vendor/invalid", "context_length": 0, "pricing": {}},
    ]
    mock_httpx(monkeypatch, lambda url, headers, params: (200, {"data": entries}))

    assert await make_catalog(FakeSettings()).list_models("openrouter") == [
        "vendor/free:free", "vendor/paid"
    ]


@pytest.mark.asyncio
async def test_http_failure_or_missing_key_falls_back_and_merges_custom(monkeypatch):
    mock_httpx(monkeypatch, lambda url, headers, params: (500, {}))
    settings = FakeSettings(
        keys={"gemini": "bad"},
        custom_models={"gemini": ["gemini-9-custom"]},
    )
    models = await make_catalog(settings).list_models("gemini")
    static = next(spec for spec in PROVIDER_SPECS if spec.name == "gemini").static_models
    assert models == ["gemini-9-custom", *static]

    no_key = await make_catalog(FakeSettings()).list_models("deepseek")
    expected = next(spec for spec in PROVIDER_SPECS if spec.name == "deepseek")
    assert no_key == list(expected.static_models)


@pytest.mark.asyncio
async def test_custom_provider_failure_uses_protocol_static_models(monkeypatch):
    mock_httpx(monkeypatch, lambda url, headers, params: (500, {}))
    catalog = make_catalog(FakeSettings(custom_models={"custom": ["gemini-9-custom"]}))

    models = await catalog.list_models_for_config(
        "custom", api_key="bad", base_url="https://relay", protocol="gemini"
    )
    static = next(spec for spec in PROVIDER_SPECS if spec.name == "gemini").static_models
    assert models == ["gemini-9-custom", *static]


def test_all_builtin_providers_have_unique_static_catalog_entries():
    names = [spec.name for spec in PROVIDER_SPECS]
    assert len(names) == len(set(names))
    assert next(spec for spec in PROVIDER_SPECS if spec.name == "gemini").static_models


@pytest.mark.asyncio
async def test_http_discovery_classifies_expected_and_unexpected_failures(monkeypatch):
    events: list[str] = []
    discovery = HttpModelDiscovery(
        log_event=lambda event, **kwargs: events.append(event),
    )

    class ExpectedClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            raise httpx.HTTPError("expected")

    monkeypatch.setattr(adapter.httpx, "AsyncClient", ExpectedClient)
    assert await discovery.fetch_models(
        "demo",
        protocol="openai",
        api_key="test-key",
        base_url="https://demo",
    ) == []

    class UnexpectedClient(ExpectedClient):
        async def get(self, *args, **kwargs):
            raise RuntimeError("unexpected")

    monkeypatch.setattr(adapter.httpx, "AsyncClient", UnexpectedClient)
    assert await discovery.fetch_models(
        "demo",
        protocol="openai",
        api_key="test-key",
        base_url="https://demo",
    ) == []
    assert events == ["catalog_fetch_failed", "catalog_fetch_unexpected"]
