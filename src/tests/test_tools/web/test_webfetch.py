import ipaddress
import sys
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import MagicMock, patch

import httpx
import pytest


from voidx.tools.web import fetch as webfetch_module
from voidx.tools.base import ToolContext
from voidx.tools.web.content import WEB_TOOL_CACHE
from voidx.tools.web.fetch import (
    PrivateHostBlocked,
    WebFetchTool,
    _contains_private_host_block,
    _fetch_url,
    _is_private_host,
)


def test_is_private_host_checks_all_dns_records():
    """_is_private_host must reject hosts where ANY A/AAAA record is private."""
    # Simulate getaddrinfo returning one public + one private address
    public = ipaddress.ip_address("93.184.216.34")
    private = ipaddress.ip_address("10.0.0.1")

    with patch("voidx.tools.web.fetch.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 80)),
            (2, 1, 6, "", ("10.0.0.1", 80)),
        ]
        assert _is_private_host("evil.example.com") is True

    # All public — should be allowed
    with patch("voidx.tools.web.fetch.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 80)),
            (2, 1, 6, "", ("1.1.1.1", 80)),
        ]
        assert _is_private_host("good.example.com") is False


def test_is_private_host_checks_ipv6_records():
    """_is_private_host must reject hosts with IPv6 private addresses."""
    with patch("voidx.tools.web.fetch.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [
            (10, 1, 6, "", ("::1", 80, 0, 0)),  # IPv6 loopback
        ]
        assert _is_private_host("ipv6loop.example.com") is True

    with patch("voidx.tools.web.fetch.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [
            (10, 1, 6, "", ("2606:4700:4700::1111", 80, 0, 0)),  # Cloudflare public
        ]
        assert _is_private_host("ipv6public.example.com") is False


def test_is_private_host_literal_ip():
    """_is_private_host must handle literal IP strings directly."""
    assert _is_private_host("127.0.0.1") is True
    assert _is_private_host("10.0.0.1") is True
    assert _is_private_host("192.168.1.1") is True
    assert _is_private_host("1.1.1.1") is False
    assert _is_private_host("::1") is True
    assert _is_private_host("2606:4700:4700::1111") is False


def test_contains_private_host_block_in_exception_chain():
    wrapped = OSError("All connection attempts failed")
    wrapped.__cause__ = PrivateHostBlocked("10.0.0.1 resolves to a private/internal address")
    outer = RuntimeError("httpx wrapped")
    outer.__cause__ = wrapped

    assert _contains_private_host_block(outer) is True


@pytest.mark.asyncio
async def test_webfetch_blocks_dns_rebinding_during_request(tmp_path, monkeypatch):
    """The connection-time resolver must be checked, not just the preflight lookup."""
    WEB_TOOL_CACHE.clear()

    class FakeResponse:
        status_code = 200
        text = "<html><body>secret</body></html>"
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            host = urlparse(url).hostname
            webfetch_module.socket.getaddrinfo(host, None)
            return FakeResponse()

    lookups = [
        [(2, 1, 6, "", ("93.184.216.34", 80))],
        [(2, 1, 6, "", ("10.0.0.1", 80))],
    ]

    def fake_getaddrinfo(*args, **kwargs):
        return lookups.pop(0)

    monkeypatch.setattr(webfetch_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(webfetch_module.socket, "getaddrinfo", fake_getaddrinfo)

    result = await WebFetchTool().execute(
        {"url": "https://rebind.example.com", "prompt": "body"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("blocked") is True
    assert "private/internal" in result.output


@pytest.mark.asyncio
async def test_fetch_url_rejects_too_many_redirects(monkeypatch):
    class RedirectResponse:
        status_code = 302
        text = ""
        headers = {"location": "/next", "content-type": "text/html"}

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            return RedirectResponse()

    monkeypatch.setattr(webfetch_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        webfetch_module,
        "_is_private_host",
        lambda host: False,
    )

    with pytest.raises(ValueError, match="too many redirects"):
        await _fetch_url("https://example.com")


class TestWebFetchRetry:
    """Tests for _fetch_url retry + 4xx/5xx classification."""

    def _make_response(self, status_code, text="body", content_type="text/html"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.headers = {"content-type": content_type}
        resp.url = "https://example.com"
        if status_code >= 400:
            resp.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError(
                    f"HTTP {status_code}", request=MagicMock(), response=resp
                )
            )
        else:
            resp.raise_for_status = MagicMock()
        return resp

    @pytest.mark.asyncio
    async def test_4xx_returns_response_not_raises(self, monkeypatch):
        resp404 = self._make_response(404, "Not Found")

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                return resp404

        monkeypatch.setattr(webfetch_module.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(webfetch_module, "_is_private_host", lambda host: False)

        result = await _fetch_url("https://example.com")
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_5xx_raises_http_status_error(self, monkeypatch):
        resp500 = self._make_response(500, "Internal Server Error")

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                return resp500

        monkeypatch.setattr(webfetch_module.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(webfetch_module, "_is_private_host", lambda host: False)

        with pytest.raises(httpx.HTTPStatusError):
            await _fetch_url("https://example.com")

    @pytest.mark.asyncio
    async def test_network_error_retried(self, monkeypatch):
        call_count = 0

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise httpx.ConnectError("connection refused")
                return self._make_response(200, "ok")

        fake = FakeClient()
        fake._make_response = self._make_response
        monkeypatch.setattr(webfetch_module.httpx, "AsyncClient", lambda *a, **kw: fake)
        monkeypatch.setattr(webfetch_module, "_is_private_host", lambda host: False)

        from voidx.config import RetryConfig
        rc = RetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=False)
        result = await webfetch_module._fetch_url_with_retry("https://example.com", rc)
        assert result.status_code == 200
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_4xx_not_retried(self, monkeypatch):
        call_count = 0

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                nonlocal call_count
                call_count += 1
                return self._make_response(404, "Not Found")

        fake = FakeClient()
        fake._make_response = self._make_response
        monkeypatch.setattr(webfetch_module.httpx, "AsyncClient", lambda *a, **kw: fake)
        monkeypatch.setattr(webfetch_module, "_is_private_host", lambda host: False)

        from voidx.config import RetryConfig
        rc = RetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=False)
        result = await webfetch_module._fetch_url_with_retry("https://example.com", rc)
        assert result.status_code == 404
        assert call_count == 1


@pytest.mark.asyncio
async def test_webfetch_timeout_uses_unified_metadata(tmp_path, monkeypatch):
    WEB_TOOL_CACHE.clear()

    async def timeout_fetch(url, retry_config):
        raise httpx.ReadTimeout("fetch timed out")

    monkeypatch.setattr(webfetch_module, "_is_private_host", lambda host: False)
    monkeypatch.setattr(webfetch_module, "_fetch_url_with_retry", timeout_fetch)

    result = await WebFetchTool().execute(
        {"url": "https://timeout.example/test", "prompt": "Extract the page"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata["error"] is True
    assert result.metadata["timeout"] is True
    assert result.metadata["error_kind"] == "tool_timeout"
    assert result.metadata["timeout_source"] == "web"


@pytest.mark.asyncio
async def test_webfetch_summary_omits_repeated_url(tmp_path, monkeypatch):
    WEB_TOOL_CACHE.clear()

    async def fake_fetch(url, retry_config):
        return webfetch_module._FetchResponse(
            url="https://example.com/a/very/long/path?with=query",
            status_code=200,
            text="<html><head><title>Example</title></head><body>Readable page text.</body></html>",
            content_type="text/html",
        )

    monkeypatch.setattr(webfetch_module, "_is_private_host", lambda host: False)
    monkeypatch.setattr(webfetch_module, "_fetch_url_with_retry", fake_fetch)

    result = await WebFetchTool().execute(
        {"url": "https://example.com/a/very/long/path?with=query", "prompt": "Extract text"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.summary == f"Fetched {len(result.output)} chars"
    assert "https://example.com" not in result.summary
    assert result.metadata["canonical_url"] == "https://example.com/a/very/long/path?with=query"
