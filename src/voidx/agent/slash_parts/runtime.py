"""Shared runtime helpers for slash commands."""

from __future__ import annotations

import sys

from voidx.ui.console import VoidConsole

ui = VoidConsole()

PROVIDERS = [
    "anthropic",
    "openai",
    "deepseek",
    "openrouter",
    "mimo",
    "qwen",
    "zhipu",
    "kimi",
    "doubao",
]


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
