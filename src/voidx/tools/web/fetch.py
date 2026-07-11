"""WebFetch tool — fetch web page content. SSRF-protected."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field

from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    model_to_json_schema,
    tool_timeout_metadata,
)
from .content import (
    WEB_TOOL_CACHE,
    cached_tool_result,
    canonicalize_url,
    extract_readable_content,
    fetch_cache_key,
)
from .mcp import call_mcp_web_tool
from voidx.tools.retry import retry_async
from voidx.config import RetryConfig

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


class PrivateHostBlocked(OSError):
    """Raised when a fetch resolves to a private/internal address."""


_DNS_RESOLUTION_LOCK = asyncio.Lock()


def _addrinfos_include_private(addrinfos) -> bool:
    for _, _, _, _, sockaddr in addrinfos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        if any(addr in net for net in _PRIVATE_RANGES):
            return True
    return False


def _is_private_host(host: str) -> bool:
    """Check if a hostname or IP resolves to a private/internal address.

    Uses getaddrinfo to check ALL A/AAAA records (not just the first one),
    preventing DNS rebinding attacks where one record is public and another is private.
    """
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _PRIVATE_RANGES)
    except ValueError:
        pass
    try:
        addrinfos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return False
    return _addrinfos_include_private(addrinfos)


@contextmanager
def _guard_private_dns():
    original_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        addrinfos = original_getaddrinfo(host, *args, **kwargs)
        if _addrinfos_include_private(addrinfos):
            raise PrivateHostBlocked(f"{host} resolves to a private/internal address")
        return addrinfos

    socket.getaddrinfo = guarded_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def _blocked_metadata(url: str, *, final_url: str | None = None) -> dict:
    metadata = {"url": url, "blocked": True}
    if final_url is not None:
        metadata["final_url"] = final_url
    return metadata


def _contains_private_host_block(exc: BaseException) -> bool:
    seen: set[int] = set()

    def visit(value: BaseException | None) -> bool:
        if value is None or id(value) in seen:
            return False
        seen.add(id(value))
        if isinstance(value, PrivateHostBlocked):
            return True
        if hasattr(value, "exceptions"):
            for item in getattr(value, "exceptions"):
                if isinstance(item, BaseException) and visit(item):
                    return True
        return visit(value.__cause__) or visit(value.__context__)

    return visit(exc)


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

    def __init__(self, settings=None, retry_config: RetryConfig | None = None):
        super().__init__()
        self._settings = settings
        self._retry_config = retry_config

    def parameters_schema(self) -> dict:
        return model_to_json_schema(WebFetchInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = WebFetchInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
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
            rc = self._retry_config or RetryConfig()
            resp = await _fetch_url_with_retry(inp.url, rc)
            if resp.status_code >= 400:
                return ToolResult(
                    output=f"HTTP {resp.status_code}: {resp.url}",
                    metadata={"url": inp.url, "final_url": resp.url, "status": resp.status_code},
                )
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
                summary=f"Fetched {len(output)} chars",
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
        except PrivateHostBlocked as e:
            return ToolResult(
                output=f"Blocked: {e}",
                metadata=_blocked_metadata(inp.url),
            )
        except Exception as e:
            if _contains_private_host_block(e):
                return ToolResult(
                    output=f"Blocked: {e}",
                    metadata=_blocked_metadata(inp.url),
                )
            if isinstance(e, httpx.TimeoutException):
                return ToolResult(
                    output=f"Failed to fetch {inp.url}: request timed out: {e}",
                    metadata=tool_timeout_metadata("web", url=inp.url),
                )
            return ToolResult(
                output=f"Failed to fetch {inp.url}: {e}",
                metadata={"url": inp.url, "error": str(e)},
            )


_MAX_REDIRECTS = 10


async def _fetch_url(url: str) -> _FetchResponse:
    async with _DNS_RESOLUTION_LOCK:
        with _guard_private_dns():
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                current_url = url
                for redirect_count in range(_MAX_REDIRECTS + 1):
                    resp = await client.get(current_url, headers={"User-Agent": "voidx/0.1"})
                    if resp.status_code not in (301, 302, 303, 307, 308):
                        break
                    location = resp.headers.get("location")
                    if not location:
                        break
                    if redirect_count >= _MAX_REDIRECTS:
                        raise ValueError(f"too many redirects fetching {url}")
                    current_url = urljoin(current_url, location)
                    redirect_host = urlparse(current_url).hostname
                    if redirect_host and _is_private_host(redirect_host):
                        raise PrivateHostBlocked(f"redirect target {redirect_host} resolves to a private/internal address")
                if resp.status_code >= 500:
                    resp.raise_for_status()
                return _FetchResponse(
                    url=current_url,
                    status_code=resp.status_code,
                    text=resp.text,
                    content_type=resp.headers.get("content-type", ""),
                )

_RETRYABLE_WEBFETCH = (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)


async def _fetch_url_with_retry(url: str, rc: RetryConfig) -> _FetchResponse:
    return await retry_async(
        lambda: _fetch_url(url),
        max_attempts=rc.max_attempts,
        base_delay=rc.base_delay,
        max_delay=rc.max_delay,
        jitter=rc.jitter,
        label=f"webfetch:{url}",
        retry_on=_RETRYABLE_WEBFETCH,
    )
