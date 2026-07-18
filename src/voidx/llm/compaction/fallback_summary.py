"""Fallback summary — basic structured summary when the compaction agent fails.

Preserves user intent plus assistant decisions and tool outcomes.  All text
extraction helpers (``_message_text``, ``_dedupe``, ``_extract_*``) live here
because they are only used by fallback summary generation.
"""

from __future__ import annotations

import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.llm.compaction.constants import (
    FALLBACK_SUMMARY_MAX_ITEMS,
    FALLBACK_SUMMARY_MAX_PER_MSG,
)
from voidx.llm.message_markers import is_guidance_message, is_step_hint_message


def message_text(msg: object) -> str:
    """Extract a flat text representation from any message content."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return str(content)


def truncate_line(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + f"... [truncated {len(compact) - limit} chars]"


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def bullets(items: list[str], *, empty: str) -> list[str]:
    if not items:
        return [f"- {empty}"]
    return [f"- {item}" for item in items]


def extract_constraint_mentions(text: str) -> list[str]:
    compact = truncate_line(text, FALLBACK_SUMMARY_MAX_PER_MSG)
    lower = compact.lower()
    markers = (
        "keep ",
        "do not ",
        "don't ",
        "must ",
        "avoid ",
        "prefer ",
        "constraint",
        "requirement",
    )
    if not any(marker in lower for marker in markers):
        return []
    parts = _split_clause_mentions(compact)
    return [part for part in parts if any(marker in part.lower() for marker in markers)] or [compact]


def extract_next_step_mentions(text: str) -> list[str]:
    compact = truncate_line(text, FALLBACK_SUMMARY_MAX_PER_MSG)
    lower = compact.lower()
    markers = (
        r"\brun\b",
        r"\btests?\b",
        r"\bverify",
        r"\bstill need",
        r"\bnext\b",
        r"\btodo\b",
        r"\bfollow up",
    )
    if not any(re.search(marker, lower) for marker in markers):
        return []
    parts = _split_clause_mentions(compact)
    return [part for part in parts if any(re.search(marker, part.lower()) for marker in markers)] or [compact]


def _split_clause_mentions(text: str) -> list[str]:
    return [part.strip(" ,.;") for part in re.split(r"[;,.]\s*", text) if part.strip(" ,.;")]


def extract_path_mentions(text: str) -> list[str]:
    paths: list[str] = []
    for raw in text.replace(",", " ").replace(")", " ").replace("(", " ").split():
        token = raw.strip(":;\"'")
        if "/" not in token:
            continue
        if token.startswith(("/", "./", "../")) or "." in token.rsplit("/", 1)[-1]:
            paths.append(token.rstrip(":;"))
    return paths


def join_with_char_budget(parts: list[str], budget: int) -> str:
    if budget <= 0:
        return ""
    kept: list[str] = []
    used = 0
    separator = "\n\n"
    for part in parts:
        extra = len(part) + (len(separator) if kept else 0)
        remaining = budget - used
        if remaining <= 0:
            break
        if extra <= remaining:
            kept.append(part)
            used += extra
            continue
        allowance = remaining - (len(separator) if kept else 0)
        if allowance > 80:
            kept.append(part[:allowance].rstrip() + "\n[conversation history truncated by char budget]")
        break
    return separator.join(kept)


def fallback_summary(messages: list) -> str:
    """Generate a basic summary from messages when the compaction agent fails.

    Preserves user intent plus assistant decisions and tool outcomes.
    """
    user_parts: list[str] = []
    assistant_parts: list[str] = []
    tool_parts: list[str] = []
    file_parts: list[str] = []
    constraint_parts: list[str] = []
    next_step_parts: list[str] = []
    for msg in messages:
        if is_step_hint_message(msg):
            continue
        content = message_text(msg).strip()
        if isinstance(msg, HumanMessage):
            if content:
                prefix = "Guidance: " if is_guidance_message(msg) else ""
                user_parts.append(prefix + truncate_line(content, FALLBACK_SUMMARY_MAX_PER_MSG))
                constraint_parts.extend(extract_constraint_mentions(content))
                next_step_parts.extend(extract_next_step_mentions(content))
                file_parts.extend(extract_path_mentions(content))
        elif isinstance(msg, AIMessage):
            if content:
                assistant_parts.append(truncate_line(content, FALLBACK_SUMMARY_MAX_PER_MSG))
                constraint_parts.extend(extract_constraint_mentions(content))
                next_step_parts.extend(extract_next_step_mentions(content))
                file_parts.extend(extract_path_mentions(content))
            for tc in getattr(msg, "tool_calls", []) or []:
                name = tc.get("name", "?")
                args = truncate_line(str(tc.get("args", {})), 160)
                assistant_parts.append(f"Called tool {name} with {args}")
        elif isinstance(msg, ToolMessage) or getattr(msg, "tool_call_id", None):
            name = getattr(msg, "name", "") or getattr(msg, "tool_call_id", "") or "tool"
            if content:
                tool_parts.append(f"{name}: {truncate_line(content, FALLBACK_SUMMARY_MAX_PER_MSG)}")
                file_parts.extend(extract_path_mentions(content))

    user_parts = dedupe(user_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]
    assistant_parts = dedupe(assistant_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]
    tool_parts = dedupe(tool_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]
    constraint_parts = dedupe(constraint_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]
    next_step_parts = dedupe(next_step_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]
    file_parts = dedupe(file_parts)[:FALLBACK_SUMMARY_MAX_ITEMS]

    lines = [
        "## Goal",
        f"- {user_parts[-1] if user_parts else '[auto-extracted from compacted context]'}",
        "",
        "## Constraints & Preferences",
    ]
    lines.extend(bullets(constraint_parts, empty="(none)"))
    lines.extend([
        "",
        "## Progress",
        "### Done",
    ])
    lines.extend(bullets(assistant_parts, empty="(none)"))
    lines.extend([
        "",
        "### In Progress",
        "- (none)",
        "",
        "### Blocked",
        "- (none)",
        "",
        "## Key Decisions",
    ])
    lines.extend(bullets([part for part in assistant_parts if part.startswith("Called tool ")], empty="(none)"))
    lines.extend([
        "",
        "## Next Steps",
    ])
    lines.extend(bullets(next_step_parts, empty="(none)"))
    lines.extend([
        "",
        "## Critical Context",
    ])
    critical = [f"User requested: {part}" for part in user_parts] + [f"Tool result: {part}" for part in tool_parts]
    lines.extend(bullets(critical, empty="(none)"))
    lines.extend([
        "",
        "## Relevant Files",
    ])
    lines.extend(bullets(file_parts, empty="(none)"))
    return "\n".join(lines)
