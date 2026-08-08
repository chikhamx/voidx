"""Ports and neutral algorithms for sanitizing external tool context."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import re
from typing import Any

ContextStripper = Callable[[Any], Any]

_SKILL_MARKER = "VOIDX_SKILL_TOOL_CONTEXT"
_SKILL_STRIPPED = "VOIDX_SKILL_TOOL_CONTEXT_STRIPPED"
_MCP_MARKER = "VOIDX_MCP_TOOL_CONTEXT"
_MCP_STRIPPED = "VOIDX_MCP_TOOL_CONTEXT_STRIPPED"
_SKILL_RE = re.compile(rf"(?m)^{_SKILL_MARKER}[ \t]*(?:\r?\n|$)")
_MCP_RE = re.compile(rf"(?m)^{_MCP_MARKER}[ \t]*(?:\r?\n|$)")


def strip_known_external_context(content: Any) -> Any:
    return _strip_content(_strip_content(content, _SKILL_RE, _skill_summary), _MCP_RE, _mcp_summary)


def compose_context_strippers(strippers: Iterable[ContextStripper]) -> ContextStripper:
    configured = tuple(strippers)

    def strip(content: Any) -> Any:
        for stripper in configured:
            content = stripper(content)
        return content

    return strip


def _strip_content(content: Any, marker: re.Pattern[str], summarize: Callable[[str], str]) -> Any:
    if isinstance(content, str):
        return _strip_text(content, marker, summarize)
    if isinstance(content, list):
        changed = False
        items: list[Any] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                text = _strip_text(item["text"], marker, summarize)
                if text != item["text"]:
                    item = {**item, "text": text}
                    changed = True
            items.append(item)
        return items if changed else content
    return content


def _strip_text(text: str, marker: re.Pattern[str], summarize: Callable[[str], str]) -> str:
    if marker.search(text) is None:
        return text
    parts = marker.split(text)
    replacement = "\n\n".join(summarize(block) for block in parts[1:])
    return f"{parts[0].rstrip()}\n\n{replacement}" if parts[0].strip() else replacement


def _skill_summary(block: str) -> str:
    matches = list(re.finditer(r"^## Skill:\s*(?P<name>.+?)\s*$", block, re.MULTILINE))
    summaries: list[str] = []
    for index, match in enumerate(matches):
        section = block[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(block)]
        source = _field_value(section, "Source") or "unknown"
        body_hash = _field_value(section, "Body-Hash") or "unknown"
        summaries.append(f"- {match.group('name').strip()} sha256={body_hash} source={source}")
    if not summaries:
        summaries.append("- active skill body omitted from historical tool result")
    return "\n".join([_SKILL_STRIPPED, *summaries])


def _mcp_summary(block: str) -> str:
    server_match = re.search(r"^## MCP Server:\s*(?P<name>.+?)\s*$", block, re.MULTILINE)
    server = server_match.group("name").strip() if server_match else ""
    tools = [match.group("tool").strip() for match in re.finditer(r"^-\s+(?P<tool>[^\s:]+)", block, re.MULTILINE)]
    if server and tools:
        detail = f"- MCP server context omitted: {server}; tools: {', '.join(tools)}"
    elif server:
        detail = f"- MCP server context omitted: {server}"
    elif tools:
        detail = f"- MCP server context omitted; tools: {', '.join(tools)}"
    else:
        detail = "- MCP server context omitted"
    return f"{_MCP_STRIPPED}\n{detail}"


def _field_value(text: str, field: str) -> str:
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""
