"""Context window management — token counting, not guessing."""

from __future__ import annotations

import tiktoken

from voidx.config.defaults import DEFAULT_MODEL


_ENCODING = None


def _get_encoding():
    global _ENCODING
    if _ENCODING is not None:
        return _ENCODING
    for name in ("cl100k_base", "o200k_base", "gpt2"):
        try:
            _ENCODING = tiktoken.get_encoding(name)
            return _ENCODING
        except Exception:
            continue
    raise RuntimeError("No tiktoken encoding available")


def count_tokens(text: str, model: str = DEFAULT_MODEL) -> int:
    """Count tokens in text using tiktoken. Deterministic, not estimated."""
    enc = _get_encoding()
    return len(enc.encode(text))


def count_messages_tokens(messages: list[dict], model: str = DEFAULT_MODEL) -> int:
    """Total token count across all messages."""
    total = 0
    for msg in messages:
        for key, value in msg.items():
            if isinstance(value, str):
                total += count_tokens(value, model)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        total += count_tokens(item, model)
                    elif isinstance(item, dict):
                        total += count_tokens(str(item), model)
    return total
