"""WebFetch tool — fetch web page content. SSRF-protected."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult

_PRIVATE_RANGES = (
    ipaddress.IPv4Network("127.0.0.0/8"),      # loopback
    ipaddress.IPv4Network("10.0.0.0/8"),       # private
    ipaddress.IPv4Network("172.16.0.0/12"),    # private
    ipaddress.IPv4Network("192.168.0.0/16"),   # private
    ipaddress.IPv4Network("169.254.0.0/16"),   # link-local
    ipaddress.IPv4Network("0.0.0.0/8"),        # current network
    ipaddress.IPv6Network("::1/128"),          # IPv6 loopback
    ipaddress.IPv6Network("fc00::/7"),         # IPv6 unique local
    ipaddress.IPv6Network("fe80::/10"),        # IPv6 link-local
)


def _is_private_host(host: str) -> bool:
    """Check if a hostname or IP resolves to a private/internal address."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        try:
            addr = ipaddress.ip_address(socket.gethostbyname(host))
        except (socket.gaierror, OSError):
            return False
    return any(addr in net for net in _PRIVATE_RANGES)


class WebFetchInput(BaseModel):
    url: str = Field(description="URL to fetch content from")
    prompt: str | None = Field(default=None, description="What to extract from the page (optional)")


class WebFetchTool(BaseTool):
    id = "webfetch"
    description = "Fetch content from a URL and convert to readable text."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(WebFetchInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = WebFetchInput.model_validate(args)

        parsed = urlparse(inp.url)
        if parsed.hostname and _is_private_host(parsed.hostname):
            return ToolResult(
                output=f"Blocked: {parsed.hostname} resolves to a private/internal address",
                metadata={"url": inp.url, "blocked": True},
            )

        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(inp.url, headers={"User-Agent": "voidx/0.1"})
                resp.raise_for_status()
                text = resp.text[:10000]  # cap at 10k chars
                if len(resp.text) > 10000:
                    text += f"\n\n[truncated: {len(resp.text)} total chars, showing first 10000]"

            return ToolResult(
                title=f"Fetched: {inp.url}",
                output=text,
                metadata={"url": inp.url, "status": resp.status_code, "size": len(resp.text)},
            )
        except Exception as e:
            return ToolResult(
                output=f"Failed to fetch {inp.url}: {e}",
                metadata={"url": inp.url, "error": str(e)},
            )
