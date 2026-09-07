"""Input boundaries for compaction summaries."""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

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
        if is_step_hint_message(original) or is_guidance_message(original):
            continue
        if is_compaction_message(original):
            eligible.append(original)
            continue
        message = _strip_turn_overlay(original)
        if isinstance(message, HumanMessage):
            content = message.content
            if _starts_with_control_marker(content):
                continue
            if isinstance(content, str) and content.strip() == _LEGACY_CONTINUATION:
                continue
        eligible.append(message)
    return eligible


def _strip_turn_overlay(message: BaseMessage) -> BaseMessage:
    content = message.content
    if not isinstance(content, str) or not content.startswith(_RUNTIME_MARKER):
        return message
    for delimiter in _TURN_DELIMITERS:
        if delimiter in content:
            return message.model_copy(update={"content": content.split(delimiter, 1)[1]})
    return message


def _starts_with_control_marker(content: object) -> bool:
    text = content if isinstance(content, str) else ""
    stripped = text.lstrip()
    if stripped.startswith((_COMPACTION_GUIDE_MARKER, _GOAL_GUIDE_MARKER)):
        return True
    return stripped.startswith(_RUNTIME_MARKER) and not any(
        delimiter in text for delimiter in _TURN_DELIMITERS
    )
