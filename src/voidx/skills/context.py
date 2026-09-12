"""Shared rendering and stripping helpers for runtime skill context."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any

from voidx.skills.schema import SkillDefinition

SKILL_TOOL_CONTEXT_MARKER = "VOIDX_SKILL_TOOL_CONTEXT"
SKILL_TOOL_CONTEXT_STRIPPED_MARKER = "VOIDX_SKILL_TOOL_CONTEXT_STRIPPED"

_SKILL_HEADER_RE = re.compile(r"^## Skill:\s*(?P<name>.+?)\s*$", re.MULTILINE)
_SKILL_TOOL_CONTEXT_MARKER_RE = re.compile(
    rf"(?m)^{re.escape(SKILL_TOOL_CONTEXT_MARKER)}[ \t]*(?:\r?\n|$)"
)
_SKILL_BLOCK_RE = re.compile(
    r'<tool_context\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</tool_context>',
    re.IGNORECASE,
)


def skill_body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def render_skill_instruction(skill: SkillDefinition) -> str:
    description = skill.meta.description.strip()
    lines = [
        f"## Skill: {skill.name}",
        f"Source: {skill.meta.scope}",
        f"Body-Hash: {skill_body_hash(skill.body)}",
        f"Path: {skill.path.resolve()}",
    ]
    if description:
        lines.append(f"Description: {description}")
    return "\n".join(lines) + f"\n\n{skill.body.strip()}"


def render_skill_tool_context(instructions: Iterable[str]) -> str:
    parts: list[str] = []
    for item in instructions:
        item_str = item.strip()
        if not item_str:
            continue
        match = _SKILL_HEADER_RE.search(item_str)
        if match:
            name = match.group("name").strip()
            parts.append(f'<tool_context type="skill" name="{name}">\n{item_str}\n</tool_context>')
        else:
            parts.append(f'<tool_context type="skill">\n{item_str}\n</tool_context>')
    return "\n\n".join(parts)


def strip_skill_tool_context(content: Any) -> Any:
    if isinstance(content, str):
        return _strip_skill_tool_context_text(content)
    if isinstance(content, list):
        changed = False
        stripped_items: list[Any] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if isinstance(text, str):
                    stripped = _strip_skill_tool_context_text(text)
                    if stripped != text:
                        item = {**item, "text": stripped}
                        changed = True
            stripped_items.append(item)
        return stripped_items if changed else content
    return content


def has_skill_tool_context(content: Any) -> bool:
    if isinstance(content, str):
        return _has_skill_tool_context_text(content)
    if isinstance(content, list):
        return any(
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            and _has_skill_tool_context_text(item["text"])
            for item in content
        )
    return False


def _has_skill_tool_context_text(text: str) -> bool:
    if _SKILL_TOOL_CONTEXT_MARKER_RE.search(text) is not None:
        return True
    for match in _SKILL_BLOCK_RE.finditer(text):
        attrs = match.group("attrs")
        if re.search(r'\btype=["\']skill["\']', attrs):
            if not re.search(r'\bstatus=["\']stripped["\']', attrs):
                return True
    return False


def _strip_skill_tool_context_text(text: str) -> str:
    def _replace_skill_tag(match: re.Match[str]) -> str:
        attrs = match.group("attrs")
        if not re.search(r'\btype=["\']skill["\']', attrs):
            return match.group(0)
        if re.search(r'\bstatus=["\']stripped["\']', attrs):
            return match.group(0)
        name_match = re.search(r'\bname=["\'](?P<name>[^"\']+)["\']', attrs)
        if name_match:
            name = name_match.group("name").strip()
            return f'<tool_context type="skill" name="{name}" status="stripped" />'
        body = match.group("body")
        header_match = _SKILL_HEADER_RE.search(body)
        if header_match:
            name = header_match.group("name").strip()
            return f'<tool_context type="skill" name="{name}" status="stripped" />'
        return '<tool_context type="skill" status="stripped" />'

    result = _SKILL_BLOCK_RE.sub(_replace_skill_tag, text)

    if _SKILL_TOOL_CONTEXT_MARKER_RE.search(result):
        parts = _SKILL_TOOL_CONTEXT_MARKER_RE.split(result)
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
        if prefix.strip():
            result = f"{prefix.rstrip()}\n\n{replacement}"
        else:
            result = replacement

    return result
