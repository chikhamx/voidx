"""Guidance message source tracking and model rendering."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Literal

from voidx.llm.message_markers import GUIDANCE_MARKER, GUIDANCE_SOURCE_MARKER

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

GuidanceSource = Literal["user", "system", "guard"]
VALID_GUIDANCE_SOURCES: frozenset[str] = frozenset({"user", "system", "guard"})
_RENDERED_MARKER = "_voidx_guidance_rendered"


def guidance_source(message: object) -> GuidanceSource | None:
    additional = getattr(message, "additional_kwargs", None)
    if not isinstance(additional, dict):
        return None
    if not additional.get(GUIDANCE_MARKER):
        return None
    source = additional.get(GUIDANCE_SOURCE_MARKER)
    if source in VALID_GUIDANCE_SOURCES:
        return source  # type: ignore[return-value]
    return None


def is_compaction_eligible_guidance(message: object) -> bool:
    return guidance_source(message) == "user"


def render_guidance_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    rendered_list: list[BaseMessage] = []
    for msg in messages:
        src = guidance_source(msg)
        if src is None:
            rendered_list.append(msg)
            continue

        additional = getattr(msg, "additional_kwargs", {}) or {}
        if additional.get(_RENDERED_MARKER):
            rendered_list.append(msg)
            continue

        raw_content = msg.content
        if isinstance(raw_content, str):
            escaped = html.escape(raw_content, quote=True)
            if src == "user":
                new_content = f"<user_guidance>{escaped}</user_guidance>"
            else:
                new_content = f'<system_guidance source="{src}">{escaped}</system_guidance>'
        else:
            new_content = raw_content

        new_additional = dict(additional)
        new_additional[_RENDERED_MARKER] = True

        msg_kwargs = {
            "content": new_content,
            "additional_kwargs": new_additional,
        }
        if hasattr(msg, "id") and msg.id is not None:
            msg_kwargs["id"] = msg.id
        if hasattr(msg, "name") and msg.name is not None:
            msg_kwargs["name"] = msg.name
        if hasattr(msg, "response_metadata") and msg.response_metadata:
            msg_kwargs["response_metadata"] = dict(msg.response_metadata)

        rendered_list.append(msg.__class__(**msg_kwargs))
    return rendered_list
