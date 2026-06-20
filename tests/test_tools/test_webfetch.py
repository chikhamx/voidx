import ipaddress
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.tools.webfetch import _is_private_host, _PRIVATE_RANGES


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
