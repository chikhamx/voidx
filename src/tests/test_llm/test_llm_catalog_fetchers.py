"""Tests for built-in provider model-list fetchers.

Covers:
  - OpenAI-compatible fetcher (used by 12 providers)
  - Anthropic fetcher
  - Gemini fetcher
  - Fallback to STATIC_MODELS when no API key or fetcher fails
  - All 14 providers have fetchers registered
"""

from __future__ import annotations

import httpx
import pytest

from voidx.llm import catalog


# ── helpers ────────────────────────────────────────────────────────────────


class FakeSettings:
    """Minimal settings stub for fetcher tests."""

    def __init__(self, keys: dict[str, str] | None = None, base_urls: dict[str, str] | None = None):
        self._keys = keys or {}
        self._base_urls = base_urls or {}

    async def resolve_api_key(self, provider: str) -> str | None:
        return self._keys.get(provider)

    async def resolve_base_url(self, provider: str) -> str | None:
        return self._base_urls.get(provider)

    async def list_custom_models(self, provider: str) -> list[str]:
        return []


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
async def test_openai_compatible_fetcher_no_key_falls_back(monkeypatch):
    """Without an API key, fetcher returns STATIC_MODELS."""
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={}))
    models = await catalog.list_models("deepseek")
    assert models == catalog.STATIC_MODELS["deepseek"]


@pytest.mark.asyncio
async def test_openai_compatible_fetcher_http_error_falls_back(monkeypatch):
    """On HTTP error, fetcher returns STATIC_MODELS."""
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={"deepseek": "sk-test"}))

    def handler(method, url, headers, params):
        return 500, {"error": "server error"}

    _mock_httpx(monkeypatch, handler)
    models = await catalog.list_models("deepseek")
    assert models == catalog.STATIC_MODELS["deepseek"]


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
    assert models == catalog.STATIC_MODELS["anthropic"]


# ── Gemini fetcher ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gemini_fetcher_parses_models(monkeypatch):
    """Gemini fetcher parses models[].name, strips 'models/' prefix."""
    monkeypatch.setattr(catalog, "_settings", FakeSettings(keys={"gemini": "AIza-test"}))

    def handler(method, url, headers, params):
        assert "generativelanguage.googleapis.com" in url
        assert params.get("key") == "AIza-test"
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
    assert models == catalog.STATIC_MODELS["gemini"]


# ── registration coverage ─────────────────────────────────────────────────


def test_all_builtin_providers_have_fetchers():
    """Every built-in provider should have a fetcher registered."""
    expected = {
        "anthropic", "openai", "openrouter",
        "deepseek", "mimo", "mimo-token-plan",
        "qwen", "zhipu", "kimi", "doubao",
        "typex", "minimax", "longcat",
        "xunfei-coding-plan", "gemini",
    }
    registered = set(catalog._fetchers.keys())
    missing = expected - registered
    assert not missing, f"Providers without fetchers: {missing}"


def test_gemini_has_static_fallback():
    """Gemini must have a STATIC_MODELS entry for fallback."""
    assert "gemini" in catalog.STATIC_MODELS
    assert len(catalog.STATIC_MODELS["gemini"]) > 0


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
