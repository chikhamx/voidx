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


_TOOL_CONTEXT_BLOCK_RE = re.compile(
    r'<tool_context\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</tool_context>',
    re.IGNORECASE,
)
_SKILL_HEADER_RE = re.compile(r"^## Skill:\s*(?P<name>.+?)\s*$", re.MULTILINE)
_MCP_SERVER_HEADER_RE = re.compile(r"^## MCP Server:\s*(?P<name>.+?)\s*$", re.MULTILINE)


def strip_known_external_context(content: Any) -> Any:
    if isinstance(content, str):
        return _strip_text(content)
    if isinstance(content, list):
        changed = False
        items: list[Any] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                text = _strip_text(item["text"])
                if text != item["text"]:
                    item = {**item, "text": text}
                    changed = True
            items.append(item)
        return items if changed else content
    return content


def compose_context_strippers(strippers: Iterable[ContextStripper]) -> ContextStripper:
    configured = tuple(strippers)

    def strip(content: Any) -> Any:
        for stripper in configured:
            content = stripper(content)
        return content

    return strip


def _strip_text(text: str) -> str:
    def _replace_tool_context(match: re.Match[str]) -> str:
        attrs = match.group("attrs")
        if re.search(r'\bstatus=["\']stripped["\']', attrs):
            return match.group(0)
        body = match.group("body")
        type_match = re.search(r'\btype=["\'](?P<type>[^"\']+)["\']', attrs)
        tool_type = type_match.group("type").strip().lower() if type_match else ""

        if tool_type == "skill":
            name_match = re.search(r'\bname=["\'](?P<name>[^"\']+)["\']', attrs)
            if name_match:
                name = name_match.group("name").strip()
                return f'<tool_context type="skill" name="{name}" status="stripped" />'
            header = _SKILL_HEADER_RE.search(body)
            if header:
                name = header.group("name").strip()
                return f'<tool_context type="skill" name="{name}" status="stripped" />'
            return '<tool_context type="skill" status="stripped" />'

        if tool_type == "mcp":
            server_match = re.search(r'\bserver=["\'](?P<server>[^"\']+)["\']', attrs)
            if server_match:
                server = server_match.group("server").strip()
                return f'<tool_context type="mcp" server="{server}" status="stripped" />'
            server_header = _MCP_SERVER_HEADER_RE.search(body)
            if server_header:
                server = server_header.group("name").strip()
                return f'<tool_context type="mcp" server="{server}" status="stripped" />'
            return '<tool_context type="mcp" status="stripped" />'

        return match.group(0)

    result = _TOOL_CONTEXT_BLOCK_RE.sub(_replace_tool_context, text)

    if _SKILL_RE.search(result):
        parts = _SKILL_RE.split(result)
        prefix = parts[0]
        replacements = []
        for block in parts[1:]:
            matches = list(_SKILL_HEADER_RE.finditer(block))
            if matches:
                for m in matches:
                    replacements.append(f'<tool_context type="skill" name="{m.group("name").strip()}" status="stripped" />')
            else:
                replacements.append('<tool_context type="skill" status="stripped" />')
        replacement = "\n\n".join(replacements)
        result = f"{prefix.rstrip()}\n\n{replacement}" if prefix.strip() else replacement

    if _MCP_RE.search(result):
        parts = _MCP_RE.split(result)
        prefix = parts[0]
        replacements = []
        for block in parts[1:]:
            server_match = _MCP_SERVER_HEADER_RE.search(block)
            if server_match:
                server = server_match.group("name").strip()
                replacements.append(f'<tool_context type="mcp" server="{server}" status="stripped" />')
            else:
                replacements.append('<tool_context type="mcp" status="stripped" />')
        replacement = "\n\n".join(replacements)
        result = f"{prefix.rstrip()}\n\n{replacement}" if prefix.strip() else replacement

    return result
