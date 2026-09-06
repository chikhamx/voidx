"""Stable upstream prompt-cache identity helpers."""

from __future__ import annotations

import hashlib
from typing import Any


def prompt_cache_key_for(
    session_id: str | None,
    provider: str,
    model: str,
    *,
    scope: str = "",
) -> str | None:
    """Return a privacy-safe key stable for one prompt-cache scope."""
    session = str(session_id or "").strip()
    provider_name = provider.strip().lower()
    model_name = model.strip().lower()
    scope_name = scope.strip().lower()
    if not session or not provider_name or not model_name:
        return None

    identity = "\x1f".join(
        ("voidx-prompt-cache", "v1", provider_name, model_name, session, scope_name)
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"voidx-v1-{digest[:55]}"


def bind_prompt_cache_key(
    model: Any,
    cache_key: str | None,
    *,
    protocol: str = "openai",
) -> Any:
    """Bind a top-level prompt_cache_key for the OpenAI chat protocol."""
    if protocol != "openai" or not cache_key:
        return model
    binder = getattr(model, "bind", None)
    if not callable(binder):
        return model
    return binder(prompt_cache_key=cache_key)
