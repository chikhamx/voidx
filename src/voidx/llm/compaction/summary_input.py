"""Input boundaries for compaction summaries."""

from __future__ import annotations

import re

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from voidx.llm.guidance import is_compaction_eligible_guidance
from voidx.llm.message_markers import (
    is_compaction_message,
    is_continuation_message,
    is_context_pressure_message,
    is_guidance_message,
    is_step_hint_message,
)

_LEGACY_CONTINUATION = "Continue if you have next steps"
_RUNTIME_MARKER = "VOIDX_RUNTIME_CONTEXT"
_COMPACTION_GUIDE_MARKER = "VOIDX_COMPACTION_GUIDE"
_GOAL_GUIDE_MARKER = "VOIDX_GOAL_RESOLUTION_GUIDE"
_TURN_DELIMITERS = ("\n\n## Task Context\n", "\n\n## User Message\n")
_TASK_TAG_PATTERN = re.compile(
    r"^\s*<(current_task_state|task)>[\s\S]*?</\1>\s*",
    re.DOTALL,
)


def compaction_summary_messages(selected_head: list[BaseMessage]) -> list[BaseMessage]:
    """Return eligible semantic messages from the source messages.

    Retains previous synthetic compaction user messages and regular
    user/assistant/tool messages, while stripping internal control messages
    (continuation, pressure hints, step hints, guidance overlays).
    """
    eligible: list[BaseMessage] = []
    for original in selected_head:
        if isinstance(original, SystemMessage):
            continue
        if is_continuation_message(original) or is_context_pressure_message(original):
            continue
        if is_step_hint_message(original):
            continue
        if is_guidance_message(original):
            if is_compaction_eligible_guidance(original):
                eligible.append(original)
            continue
        if is_compaction_message(original):
            eligible.append(original)
            continue
        if _starts_with_control_marker(original.content):
            continue
        message = _strip_turn_overlay(original)
        if isinstance(message, HumanMessage):
            content = message.content
            if _starts_with_control_marker(content):
                continue
            if isinstance(content, str) and not content.strip():
                continue
            if isinstance(content, list) and not content:
                continue
            if isinstance(content, str) and content.strip() == _LEGACY_CONTINUATION:
                continue
        eligible.append(message)
    return eligible


def _strip_turn_overlay_text(content: str) -> str:
    match = _TASK_TAG_PATTERN.match(content)
    if match:
        return content[match.end():]
    if not content.startswith(_RUNTIME_MARKER):
        return content
    for delimiter in _TURN_DELIMITERS:
        if delimiter in content:
            return content.split(delimiter, 1)[1]
    for marker in ("## Task Context", "## User Message"):
        header = f"\n\n{marker}"
        if header in content:
            return content.split(header, 1)[1].lstrip("\r\n")
    return content


def _strip_turn_overlay(message: BaseMessage) -> BaseMessage:
    content = message.content
    if isinstance(content, str):
        stripped = _strip_turn_overlay_text(content)
        if stripped != content:
            return message.model_copy(update={"content": stripped})
    elif isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
            if isinstance(text, str):
                stripped = _strip_turn_overlay_text(text)
                if stripped != text:
                    if stripped:
                        stripped_first = {**first, "text": stripped}
                        return message.model_copy(update={"content": [stripped_first, *content[1:]]})
                    return message.model_copy(update={"content": list(content[1:])})
    return message


def _starts_with_control_marker(content: object) -> bool:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and len(content) == 1:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            first_text = first.get("text", "")
            if isinstance(first_text, str):
                text = first_text
            else:
                return False
        else:
            return False
    else:
        return False
    if not text:
        return False
    stripped = text.lstrip()
    if stripped.startswith((_COMPACTION_GUIDE_MARKER, _GOAL_GUIDE_MARKER)):
        return True
    match = _TASK_TAG_PATTERN.match(stripped)
    if match and not stripped[match.end():].strip():
        return True
    return stripped.startswith(_RUNTIME_MARKER) and not any(
        delimiter in text for delimiter in _TURN_DELIMITERS
    )
