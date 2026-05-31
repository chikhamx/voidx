"""WebFetch tool — fetch web page content. SSRF-protected."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult
from voidx.tools.web_content import (
    WEB_TOOL_CACHE,
    cached_tool_result,
    canonicalize_url,
    extract_readable_content,
    fetch_cache_key,
)
from voidx.tools.web_mcp import call_mcp_web_tool

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
    prompt: str = Field(description="The prompt to run on the fetched content")
    max_chars: int = Field(default=12000, ge=1000, le=50000, description="Maximum extracted text characters")


@dataclass
class _FetchResponse:
    url: str
    status_code: int
    text: str
    content_type: str


class WebFetchTool(BaseTool):
    id = "webfetch"
    description = "Fetch content from a URL and convert to readable text."

    def __init__(self, settings=None):
        self._settings = settings

    def parameters_schema(self) -> dict:
        return model_to_json_schema(WebFetchInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = WebFetchInput.model_validate(args)
        mcp_arguments = inp.model_dump(exclude_none=True)
        if "max_chars" not in args:
            mcp_arguments.pop("max_chars", None)
        mcp_result = await call_mcp_web_tool(
            kind="fetch",
            settings=self._settings,
            ctx=ctx,
            arguments=mcp_arguments,
            title=f"Fetched: {inp.url}",
        )
        if mcp_result is not None:
            return mcp_result

        parsed = urlparse(inp.url)
        if parsed.scheme not in {"http", "https"}:
            return ToolResult(
                output=f"Blocked: unsupported URL scheme '{parsed.scheme or '(none)'}'",
                metadata={"url": inp.url, "blocked": True, "reason": "unsupported_scheme"},
            )
        if parsed.hostname and _is_private_host(parsed.hostname):
            return ToolResult(
                output=f"Blocked: {parsed.hostname} resolves to a private/internal address",
                metadata={"url": inp.url, "blocked": True},
            )

        key = fetch_cache_key(inp.url, inp.prompt, inp.max_chars)
        cached = WEB_TOOL_CACHE.get(key)
        if isinstance(cached, ToolResult):
            return cached_tool_result(cached)

        try:
            resp = await _fetch_url(inp.url)
            final_host = urlparse(resp.url).hostname
            if final_host and _is_private_host(final_host):
                return ToolResult(
                    output=f"Blocked: redirect target {final_host} resolves to a private/internal address",
                    metadata={"url": inp.url, "final_url": resp.url, "blocked": True},
                )
            extracted = extract_readable_content(
                url=resp.url,
                text=resp.text,
                content_type=resp.content_type,
                prompt=inp.prompt,
                max_chars=inp.max_chars,
            )
            output = extracted["content"] or resp.text[:inp.max_chars]

            result = ToolResult(
                title=f"Fetched: {canonicalize_url(resp.url)}",
                output=output,
                metadata={
                    "url": inp.url,
                    "canonical_url": extracted["url"],
                    "status": resp.status_code,
                    "size": len(resp.text),
                    "content_type": resp.content_type,
                    "title": extracted["title"],
                    "extracted_chars": extracted["total_chars"],
                    "truncated": extracted["truncated"],
                    "excerpt_count": extracted["excerpt_count"],
                    "cached": False,
                },
            )
            WEB_TOOL_CACHE.set(key, result, ttl_seconds=1800)
            return result
        except Exception as e:
            return ToolResult(
                output=f"Failed to fetch {inp.url}: {e}",
                metadata={"url": inp.url, "error": str(e)},
            )


async def _fetch_url(url: str) -> _FetchResponse:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "voidx/0.1"})
        resp.raise_for_status()
        return _FetchResponse(
            url=str(resp.url),
            status_code=resp.status_code,
            text=resp.text,
            content_type=resp.headers.get("content-type", ""),
        )
