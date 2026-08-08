"""Tests for built-in provider model-list fetchers.

Covers:
  - OpenAI-compatible fetcher (used by 12 providers)
  - Anthropic fetcher
  - Gemini fetcher
  - Fallback to explicit provider static models when fetching fails
  - All built-in providers have explicit catalog entries
"""

from __future__ import annotations

import httpx
import pytest

from voidx.llm import catalog
from voidx.llm.providers.catalog import PROVIDER_SPECS


def _static(provider: str) -> list[str]:
    spec = next(item for item in PROVIDER_SPECS if item.name == provider)
    return list(spec.static_models)


# ── helpers ────────────────────────────────────────────────────────────────


class FakeSettings:
    """Minimal settings stub for fetcher tests."""

    def __init__(
        self,
        keys: dict[str, str] | None = None,
        base_urls: dict[str, str] | None = None,
        custom_models: dict[str, list[str]] | None = None,
    ):
        self._keys = keys or {}
        self._base_urls = base_urls or {}
        self._custom_models = custom_models or {}

    async def resolve_api_key(self, provider: str) -> str | None:
        return self._keys.get(provider)

    async def resolve_base_url(self, provider: str) -> str | None:
        return self._base_urls.get(provider)

    async def list_custom_models(self, provider: str) -> list[str]:
        return self._custom_models.get(provider, [])


def _mock_httpx(monkeypatch, handler):
    """Replace httpx.AsyncClient with a mock that delegates to *handler*.

    *handler* receives (method, url, headers, params) and returns
    (status_code, json_body).
    """

    class FakeResponse:
        def __init__(self, status_code, json_body):
            self.status_code = status_code
            self._json = json_body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=None, response=self)

        def json(self):
            return self._json

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, *, headers=None, params=None):
            method = "GET"
            status, body = handler(method, url, headers or {}, params or {})
            return FakeResponse(status, body)

    monkeypatch.setattr(catalog.httpx, "AsyncClient", FakeClient)


# ── OpenAI-compatible fetcher ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_compatible_fetcher_parses_models(monkeypatch):
    """OpenAI-compatible fetcher parses data[].id from /models."""
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={"deepseek": "sk-test"}))

    def handler(method, url, headers, params):
        assert url == "https://api.deepseek.com/v1/models"
        assert headers.get("Authorization") == "Bearer sk-test"
        return 200, {"data": [
            {"id": "deepseek-v4-pro"},
            {"id": "deepseek-v4-flash"},
            {"id": "text-embedding-xxx"},  # should be filtered
        ]}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models("deepseek")
    assert "deepseek-v4-pro" in models
    assert "deepseek-v4-flash" in models
    assert "text-embedding-xxx" not in models


@pytest.mark.asyncio
async def test_openai_compatible_fetcher_uses_custom_base_url(monkeypatch):
    """Fetcher respects user-configured base_url from settings."""
    monkeypatch.setattr(catalog, "_settings", FakeSettings(
        keys={"deepseek": "sk-test"},
        base_urls={"deepseek": "https://my-relay.example.com/v1"},
    ))

    def handler(method, url, headers, params):
        assert url == "https://my-relay.example.com/v1/models"
        return 200, {"data": [{"id": "custom-model"}]}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models("deepseek")
    assert "custom-model" in models


@pytest.mark.asyncio
async def test_fetched_models_are_sorted_latest_first(monkeypatch):
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={"openai": "sk-test"}))

    def handler(method, url, headers, params):
        return 200, {"data": [
            {"id": "gpt-4o"},
            {"id": "gpt-5.4-mini"},
            {"id": "gpt-5.10-mini"},
            {"id": "gpt-5.4-nano"},
        ]}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models("openai")

    assert models == ["gpt-5.10-mini", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-4o"]


@pytest.mark.asyncio
async def test_openai_compatible_fetcher_no_key_falls_back(monkeypatch):
    """Without an API key, fetcher returns the provider spec fallback."""
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={}))
    models = await catalog.list_models("deepseek")
    assert models == _static("deepseek")


@pytest.mark.asyncio
async def test_openai_compatible_fetcher_http_error_falls_back(monkeypatch):
    """On HTTP error, fetcher returns the provider spec fallback."""
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={"deepseek": "sk-test"}))

    def handler(method, url, headers, params):
        return 500, {"error": "server error"}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models("deepseek")
    assert models == _static("deepseek")


# ── Anthropic fetcher ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_fetcher_parses_models(monkeypatch):
    """Anthropic fetcher parses data[].id from /v1/models with x-api-key header."""
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={"anthropic": "sk-ant-test"}))

    def handler(method, url, headers, params):
        assert url == "https://api.anthropic.com/v1/models"
        assert headers.get("x-api-key") == "sk-ant-test"
        assert "anthropic-version" in headers
        return 200, {"data": [
            {"id": "claude-opus-4-8"},
            {"id": "claude-sonnet-4-6"},
        ]}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models("anthropic")
    assert "claude-opus-4-8" in models
    assert "claude-sonnet-4-6" in models


@pytest.mark.asyncio
async def test_anthropic_fetcher_no_key_falls_back(monkeypatch):
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={}))
    models = await catalog.list_models("anthropic")
    assert models == _static("anthropic")


# ── Gemini fetcher ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gemini_fetcher_parses_models(monkeypatch):
    """Gemini fetcher parses models[].name, strips 'models/' prefix."""
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={"gemini": "AIza-test"}))

    def handler(method, url, headers, params):
        assert "generativelanguage.googleapis.com" in url
        assert params.get("key") == "AIza-test"
        assert headers.get("x-goog-api-key") == "AIza-test"
        return 200, {"models": [
            {"name": "models/gemini-2.5-flash"},
            {"name": "models/gemini-2.5-pro"},
            {"name": "models/text-embedding-004"},  # should be filtered
        ]}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models("gemini")
    assert "gemini-2.5-flash" in models
    assert "gemini-2.5-pro" in models
    assert "text-embedding-004" not in models


@pytest.mark.asyncio
async def test_gemini_fetcher_no_key_falls_back(monkeypatch):
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={}))
    models = await catalog.list_models("gemini")
    assert models == _static("gemini")


@pytest.mark.asyncio
async def test_gemini_fetcher_strips_v1beta_suffix_from_custom_base_url(monkeypatch):
    """Custom base_url ending with /v1beta should not produce /v1beta/v1beta/models."""
    monkeypatch.setattr(catalog, "_settings", FakeSettings(
        keys={"gemini": "AIza-test"},
        base_urls={"gemini": "http://relay.example.com/antigravity/v1beta"},
    ))

    captured_url = []

    def handler(method, url, headers, params):
        captured_url.append(url)
        return 200, {"models": [{"name": "models/gemini-2.5-pro"}]}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models("gemini")
    assert captured_url == ["http://relay.example.com/antigravity/v1beta/models"]
    assert "gemini-2.5-pro" in models


@pytest.mark.asyncio
async def test_configured_custom_provider_fetches_models_with_protocol_base_url_and_key(monkeypatch):
    monkeypatch.setattr(catalog, "_settings", FakeSettings())
    captured_url = []

    def handler(method, url, headers, params):
        captured_url.append(url)
        assert params.get("key") == "AIza-temp"
        assert headers.get("x-goog-api-key") == "AIza-temp"
        return 200, {"models": [{"name": "models/gemini-3.1-pro-high"}]}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models_for_config(
        "jochen",
        api_key="AIza-temp",
        base_url="http://relay.example.com/antigravity/v1beta",
        protocol="gemini",
    )

    assert captured_url == ["http://relay.example.com/antigravity/v1beta/models"]
    assert models == ["gemini-3.1-pro-high"]


@pytest.mark.asyncio
async def test_configured_custom_provider_falls_back_to_local_models(monkeypatch):
    monkeypatch.setattr(
        catalog,
        "_settings",
        FakeSettings(custom_models={"jochen": ["models/gemini-3.1-pro-high"]}),
    )

    def handler(method, url, headers, params):
        return 500, {}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models_for_config(
        "jochen",
        api_key="AIza-temp",
        base_url="http://relay.example.com/antigravity",
        protocol="gemini",
    )

    assert models == ["models/gemini-3.1-pro-high"] + _static("gemini")


@pytest.mark.asyncio
async def test_configured_custom_provider_falls_back_to_protocol_static_models(monkeypatch):
    monkeypatch.setattr(catalog, "_settings", FakeSettings())

    def handler(method, url, headers, params):
        return 500, {}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models_for_config(
        "jochen",
        api_key="AIza-temp",
        base_url="http://relay.example.com/antigravity",
        protocol="gemini",
    )

    assert models == _static("gemini")


# ── explicit catalog coverage ─────────────────────────────────────────────


def test_all_builtin_providers_have_static_catalog_entries():
    names = {spec.name for spec in PROVIDER_SPECS}
    assert names == {
        "anthropic", "openai", "openrouter",
        "deepseek", "mimo", "mimo-token-plan",
        "qwen", "zhipu", "kimi", "doubao",
        "typex", "minimax", "longcat",
        "xunfei-coding-plan", "gemini",
    }


def test_gemini_has_static_fallback():
    assert _static("gemini")


@pytest.mark.asyncio
async def test_resolve_base_url_logs_settings_failure(monkeypatch):
    class FailingSettings:
        async def resolve_base_url(self, provider: str) -> str | None:
            raise RuntimeError("settings failed")

    events = []

    def fake_log_tool_event(event, *, tool_name="", message="", **kwargs):
        events.append((event, tool_name, message))

    monkeypatch.setattr(catalog, "_settings", FailingSettings())
    monkeypatch.setattr(catalog, "log_tool_event", fake_log_tool_event)

    assert await catalog._resolve_base_url("deepseek") == "https://api.deepseek.com/v1"
    assert events == [("llm_resolve_base_url", "catalog", "settings failed")]


@pytest.mark.asyncio
async def test_resolve_api_key_logs_settings_failure(monkeypatch):
    class FailingSettings:
        async def resolve_api_key(self, provider: str) -> str | None:
            raise RuntimeError("settings failed")

    events = []

    def fake_log_tool_event(event, *, tool_name="", message="", **kwargs):
        events.append((event, tool_name, message))

    monkeypatch.setattr(catalog, "_settings", FailingSettings())
    monkeypatch.setattr(catalog, "log_tool_event", fake_log_tool_event)

    assert await catalog._resolve_api_key("deepseek") is None
    assert events == [("llm_resolve_api_key", "catalog", "settings failed")]
