"""Shared runtime helpers for slash commands."""

from __future__ import annotations

import sys

from voidx.agent.graph.runtime import ui

_STATIC_PROVIDERS = [
    "anthropic",
    "openai",
    "deepseek",
    "openrouter",
    "mimo",
    "mimo-token-plan",
    "qwen",
    "zhipu",
    "kimi",
    "doubao",
]


async def get_providers(settings=None) -> list[str]:
    """Return providers list, merging static + custom providers from settings."""
    base = list(_STATIC_PROVIDERS)
    if settings:
        for profile in await settings.list_profiles():
            if profile.provider not in base:
                base.append(profile.provider)
        for cp in settings.list_custom_providers():
            name = cp["name"]
            if name not in base:
                base.append(name)
    return base


# Backward-compatible alias (static list only).
PROVIDERS = list(_STATIC_PROVIDERS)


def _w(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


async def _select_from_list(app, prompt: str, items: list[str]) -> int | None:
    if not app or not items:
        return None
    choices = [(item, str(i), "") for i, item in enumerate(items)]
    res = await app.ask_choice(prompt, choices)
    if res is not None:
        return int(res)
    return None
