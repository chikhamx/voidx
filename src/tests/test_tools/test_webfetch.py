import ipaddress
import sys
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

import pytest


from voidx.tools import webfetch as webfetch_module
from voidx.tools.base import ToolContext
from voidx.tools.web_content import WEB_TOOL_CACHE
from voidx.tools.webfetch import (
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

    with patch("voidx.tools.webfetch.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 80)),
            (2, 1, 6, "", ("10.0.0.1", 80)),
        ]
        assert _is_private_host("evil.example.com") is True

    # All public — should be allowed
    with patch("voidx.tools.webfetch.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 80)),
            (2, 1, 6, "", ("1.1.1.1", 80)),
        ]
        assert _is_private_host("good.example.com") is False


def test_is_private_host_checks_ipv6_records():
    """_is_private_host must reject hosts with IPv6 private addresses."""
    with patch("voidx.tools.webfetch.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [
            (10, 1, 6, "", ("::1", 80, 0, 0)),  # IPv6 loopback
        ]
        assert _is_private_host("ipv6loop.example.com") is True

    with patch("voidx.tools.webfetch.socket.getaddrinfo") as mock_gai:
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
