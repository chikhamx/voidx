"""Thinking/reasoning extraction from LLM stream chunks.

Each protocol surfaces reasoning tokens differently:
  - ``anthropic`` — thinking blocks in ``content`` + ``response_metadata``
  - ``openai`` / ``deepseek`` — ``reasoning_content`` in ``additional_kwargs``
  - ``gemini``    — tries the anthropic path first, then the openai path

:func:`extract_thinking` is the single entry point; protocol dispatch happens
there.  The helper functions are module-private.
"""

from __future__ import annotations

from langchain_core.messages import AIMessageChunk

_THINKING_BLOCK_TYPES = {
    "thinking",
    "redacted_thinking",
    "reasoning",
    "reasoning_content",
}


def _extract_reasoning_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_extract_reasoning_text(item) for item in value)
    if not isinstance(value, dict):
        return ""

    parts: list[str] = []
    for key in ("thinking", "reasoning_content", "reasoning", "text", "data"):
        field = value.get(key)
        if isinstance(field, str):
            parts.append(field)

    summary = value.get("summary")
    if isinstance(summary, (dict, list)):
        parts.append(_extract_reasoning_text(summary))

    return "".join(parts)


def _extract_reasoning_blocks(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in _THINKING_BLOCK_TYPES:
            parts.append(_extract_reasoning_text(item))
    return "".join(parts)


def _extract_thinking_anthropic(chunk: AIMessageChunk) -> str:
    parts: list[str] = []
    content_text = _extract_reasoning_blocks(chunk.content)
    if content_text:
        parts.append(content_text)

    meta = chunk.response_metadata
    if isinstance(meta, dict):
        for key in ("thinking", "reasoning"):
            text = _extract_reasoning_text(meta.get(key))
            if text:
                parts.append(text)
    return "".join(parts)


def _extract_thinking_openai(chunk: AIMessageChunk) -> str:
    parts: list[str] = []
    content_text = _extract_reasoning_blocks(chunk.content)
    if content_text:
        parts.append(content_text)

    extra = chunk.additional_kwargs
    if isinstance(extra, dict):
        for key in ("reasoning_content", "reasoning", "thinking", "reasoning_details"):
            text = _extract_reasoning_text(extra.get(key))
            if text:
                parts.append(text)
    return "".join(parts)


def extract_thinking(chunk: AIMessageChunk, protocol: str) -> str:
    if protocol == "anthropic":
        return _extract_thinking_anthropic(chunk)
    if protocol == "gemini":
        return _extract_thinking_anthropic(chunk) or _extract_thinking_openai(chunk)
    # Both openai and deepseek protocols use the OpenAI-compatible
    # extraction path (reasoning_content in additional_kwargs).
    return _extract_thinking_openai(chunk)
