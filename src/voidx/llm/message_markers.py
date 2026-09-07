"""Synthetic message markers shared by LLM context services."""

from __future__ import annotations

STEP_HINT_MARKER = "_voidx_step_hint"
GUIDANCE_MARKER = "_voidx_guidance"
CONTEXT_PRESSURE_MARKER = "_voidx_context_pressure"
COMPACTION_MESSAGE_MARKER = "_voidx_compaction_message"
CONTINUATION_MESSAGE_MARKER = "_voidx_continuation"
DEFAULT_CONTINUATION_TEXT = "Continue if you have next steps."


def is_step_hint_message(message: object) -> bool:
    return bool(getattr(message, "additional_kwargs", {}).get(STEP_HINT_MARKER))


def is_guidance_message(message: object) -> bool:
    return bool(getattr(message, "additional_kwargs", {}).get(GUIDANCE_MARKER))


def is_context_pressure_message(message: object) -> bool:
    return bool(getattr(message, "additional_kwargs", {}).get(CONTEXT_PRESSURE_MARKER))


def is_compaction_message(message: object) -> bool:
    return bool(getattr(message, "additional_kwargs", {}).get(COMPACTION_MESSAGE_MARKER))


def is_continuation_message(message: object) -> bool:
    return bool(getattr(message, "additional_kwargs", {}).get(CONTINUATION_MESSAGE_MARKER))


def create_compaction_user_message(
    content: str,
    metadata: object,
    *,
    id: str | None = None,
) -> object:
    from langchain_core.messages import HumanMessage

    extra: dict[str, object] = {
        COMPACTION_MESSAGE_MARKER: True,
    }
    if hasattr(metadata, "model_dump"):
        extra.update(metadata.model_dump())
    elif isinstance(metadata, dict):
        extra.update(metadata)
    return HumanMessage(content=content, additional_kwargs=extra, id=id)


def create_continuation_message(
    content: str = DEFAULT_CONTINUATION_TEXT,
    *,
    id: str | None = None,
) -> object:
    from langchain_core.messages import HumanMessage

    return HumanMessage(
        content=content,
        additional_kwargs={CONTINUATION_MESSAGE_MARKER: True},
        id=id,
    )
