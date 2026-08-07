"""Shared helpers for web search/fetch tools."""

from __future__ import annotations

import copy
import html
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, parse_qs, urlencode, unquote, urlparse, urlunparse

from voidx.tooling.domain.result import ToolResult

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "ref",
    "spm",
}
_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}
_BLOCK_TAGS = {
    "article",
    "aside",
    "blockquote",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_STOP_WORDS = {
    "about",
    "after",
    "before",
    "content",
    "extract",
    "from",
    "only",
    "page",
    "show",
    "this",
    "with",
}


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class WebToolCache:
    def __init__(self, max_entries: int = 256) -> None:
        self._items: dict[str, _CacheEntry] = {}
        self._max_entries = max_entries

    def get(self, key: str) -> Any | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._items.pop(key, None)
            return None
        return copy.deepcopy(entry.value)

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if len(self._items) >= self._max_entries:
            oldest = min(self._items, key=lambda item: self._items[item].expires_at)
            self._items.pop(oldest, None)
        self._items[key] = _CacheEntry(copy.deepcopy(value), time.monotonic() + ttl_seconds)

    def clear(self) -> None:
        self._items.clear()


WEB_TOOL_CACHE = WebToolCache()


def cached_tool_result(result: ToolResult) -> ToolResult:
    metadata = dict(result.metadata or {})
    metadata["cached"] = True
    return result.model_copy(deep=True, update={"metadata": metadata})


def canonicalize_url(url: str) -> str:
    url = _decode_duckduckgo_url(url.strip())
    parsed = urlparse(url)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    netloc = host
    if parsed.port and not (
        scheme == "http" and parsed.port == 80
        or scheme == "https" and parsed.port == 443
    ):
        netloc = f"{host}:{parsed.port}"
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_items), doseq=True)
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", query, ""))


def domain_for_url(url: str) -> str:
    host = urlparse(canonicalize_url(url)).hostname or ""
    return host.removeprefix("www.")


def matches_domain(url: str, domain: str) -> bool:
    host = domain_for_url(url)
    target = domain.lower().strip().removeprefix("www.")
    return host == target or host.endswith(f".{target}")


def normalize_search_results(results: list[dict[str, str]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for rank, result in enumerate(results, start=1):
        raw_url = result.get("url", "").strip()
        if not raw_url:
            continue
        url = canonicalize_url(raw_url)
        if url in seen:
            continue
        seen.add(url)
        title = _clean_inline_text(result.get("title", "")) or domain_for_url(url) or url
        snippet = _clean_inline_text(result.get("snippet", ""))
        normalized.append({
            "rank": rank,
            "title": title,
            "url": url,
            "domain": domain_for_url(url),
            "snippet": snippet,
        })
    return normalized


def extract_readable_content(
    *,
    url: str,
    text: str,
    content_type: str = "",
    prompt: str = "",
    max_chars: int = 12_000,
) -> dict[str, Any]:
    if "html" in content_type.lower() or _looks_like_html(text):
        parsed = _ReadableHtmlParser()
        parsed.feed(text)
        parsed.close()
        title = _clean_inline_text(parsed.title)
        blocks = _dedupe_blocks(parsed.blocks)
    else:
        title = ""
        blocks = _plain_text_blocks(text)

    page_text = "\n\n".join(blocks).strip()
    relevant = _relevant_blocks(blocks, prompt)
    if relevant:
        page_text = "## Relevant excerpts\n\n" + "\n\n".join(relevant) + "\n\n## Page content\n\n" + page_text

    total_chars = len(page_text)
    truncated = total_chars > max_chars
    if truncated:
        page_text = page_text[:max_chars].rstrip()
        page_text += f"\n\n[truncated: {total_chars} extracted chars, showing first {max_chars}]"

    return {
        "title": title,
        "url": canonicalize_url(url),
        "content": page_text,
        "total_chars": total_chars,
        "truncated": truncated,
        "excerpt_count": len(relevant),
    }


def search_cache_key(
    *,
    query: str,
    allowed_domains: list[str] | None,
    blocked_domains: list[str] | None,
    max_results: int,
    backend: str,
) -> str:
    return "|".join([
        "search:v2",
        backend,
        _clean_inline_text(query).lower(),
        ",".join(sorted((allowed_domains or []))),
        ",".join(sorted((blocked_domains or []))),
        str(max_results),
    ])


def fetch_cache_key(url: str, prompt: str, max_chars: int) -> str:
    return "|".join(["fetch:v2", canonicalize_url(url), _clean_inline_text(prompt).lower(), str(max_chars)])


class _ReadableHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.title = ""
        self._title_parts: list[str] = []
        self._current_parts: list[str] = []
        self._current_tag = ""
        self._heading_level = 0
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            self._title_parts = []
            return
        if self._skip_depth:
            return
        if tag == "br":
            self._append_text("\n")
            return
        if tag in _BLOCK_TAGS or re.fullmatch(r"h[1-6]", tag):
            self._finish_block()
            self._current_tag = tag
            self._heading_level = int(tag[1]) if re.fullmatch(r"h[1-6]", tag) else 0

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self.title = _clean_inline_text(" ".join(self._title_parts))
            self._in_title = False
            return
        if self._skip_depth:
            return
        if tag == self._current_tag or tag in _BLOCK_TAGS or re.fullmatch(r"h[1-6]", tag):
            self._finish_block()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if data.strip():
            self._append_text(data)

    def close(self) -> None:
        self._finish_block()
        super().close()

    def _append_text(self, text: str) -> None:
        if not self._current_tag:
            self._current_tag = "p"
        self._current_parts.append(text)

    def _finish_block(self) -> None:
        raw = " ".join(self._current_parts)
        text = _clean_block_text(raw)
        if text:
            if self._heading_level:
                text = f"{'#' * self._heading_level} {text}"
            elif self._current_tag == "li":
                text = f"- {text}"
            self.blocks.append(text)
        self._current_parts = []
        self._current_tag = ""
        self._heading_level = 0


def _decode_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    if "duckduckgo.com" not in (parsed.hostname or ""):
        return url
    values = parse_qs(parsed.query).get("uddg")
    if values:
        return unquote(values[0])
    return url


def _looks_like_html(text: str) -> bool:
    head = text[:500].lower()
    return "<html" in head or "<body" in head or "<!doctype html" in head


def _plain_text_blocks(text: str) -> list[str]:
    return [
        _clean_block_text(block)
        for block in re.split(r"\n\s*\n", text)
        if _clean_block_text(block)
    ]


def _dedupe_blocks(blocks: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        normalized = _clean_inline_text(block).lower()
        if len(normalized) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(block)
    return result


def _relevant_blocks(blocks: list[str], prompt: str) -> list[str]:
    terms = [
        term.lower()
        for term in re.findall(r"[A-Za-z0-9_.-]{3,}|[\u4e00-\u9fff]{2,}", prompt)
        if term.lower() not in _STOP_WORDS
    ]
    if not terms:
        return []
    matches = [
        block
        for block in blocks
        if any(term in block.lower() for term in terms)
    ]
    return matches[:5]


def _clean_inline_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(text))).strip()


def _clean_block_text(text: str) -> str:
    lines = [_clean_inline_text(line) for line in str(text).splitlines()]
    return "\n".join(line for line in lines if line).strip()
