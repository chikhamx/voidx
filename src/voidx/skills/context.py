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
    body = "\n\n".join(item.strip() for item in instructions if item.strip())
    if not body:
        return ""
    return f"{SKILL_TOOL_CONTEXT_MARKER}\nScope: current-turn\n\n{body}"


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
        return _SKILL_TOOL_CONTEXT_MARKER_RE.search(content) is not None
    if isinstance(content, list):
        return any(
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            and _SKILL_TOOL_CONTEXT_MARKER_RE.search(item["text"]) is not None
            for item in content
        )
    return False


def _strip_skill_tool_context_text(text: str) -> str:
    if _SKILL_TOOL_CONTEXT_MARKER_RE.search(text) is None:
        return text
    parts = _SKILL_TOOL_CONTEXT_MARKER_RE.split(text)
    prefix = parts[0]
    replacements = [_stripped_summary(block) for block in parts[1:]]
    replacement = "\n\n".join(replacements)
    if prefix.strip():
        return f"{prefix.rstrip()}\n\n{replacement}"
    return replacement


def _stripped_summary(block: str) -> str:
    summaries = _skill_block_summaries(block)
    if not summaries:
        summaries = ["- active skill body omitted from historical tool result"]
    return "\n".join([SKILL_TOOL_CONTEXT_STRIPPED_MARKER, *summaries])


def _skill_block_summaries(block: str) -> list[str]:
    matches = list(_SKILL_HEADER_RE.finditer(block))
    summaries: list[str] = []
    for index, match in enumerate(matches):
        name = match.group("name").strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        section = block[match.end():end]
        source = _field_value(section, "Source") or "unknown"
        body_hash = _field_value(section, "Body-Hash") or "unknown"
        summaries.append(f"- {name} sha256={body_hash} source={source}")
    return summaries


def _field_value(text: str, field: str) -> str:
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""
