"""Context-window token estimation."""

from __future__ import annotations

from functools import lru_cache

import tiktoken

from voidx.config.defaults import DEFAULT_MODEL


@lru_cache(maxsize=64)
def _get_encoding(model: str = ""):
    """Resolve a model tokenizer, falling back to a stable base encoding."""
    if model:
        try:
            return tiktoken.encoding_for_model(model)
        except (KeyError, ValueError):
            return _get_encoding("")
    for name in ("cl100k_base", "o200k_base", "gpt2"):
        try:
            return tiktoken.get_encoding(name)
        except Exception:
            continue
    raise RuntimeError("No tiktoken encoding available")


def count_tokens(text: str, model: str = DEFAULT_MODEL) -> int:
    """Estimate text tokens with the model tokenizer or a stable fallback."""
    return len(_get_encoding(model).encode(text))


def count_messages_tokens(messages: list[dict], model: str = DEFAULT_MODEL) -> int:
    """Estimate tokens across serializable message fields."""
    total = 0
    for msg in messages:
        for value in msg.values():
            if isinstance(value, str):
                total += count_tokens(value, model)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        total += count_tokens(item, model)
                    elif isinstance(item, dict):
                        total += count_tokens(str(item), model)
    return total
