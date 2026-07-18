"""Shared runtime helpers for slash commands."""

from __future__ import annotations

import asyncio
import sys

def _builtin_providers() -> list[str]:
    from voidx.llm.providers import all_specs

    return [spec.name for spec in all_specs()]


async def get_providers(settings=None) -> list[str]:
    """Return registered providers merged with custom settings providers."""
    base = _builtin_providers()
    if settings:
        for profile in await settings.list_profiles():
            if profile.provider not in base:
                base.append(profile.provider)
        for cp in settings.list_custom_providers():
            name = cp["name"]
            if name not in base:
                base.append(name)
    return base


class _ProviderNames:
    def __iter__(self):
        return iter(_builtin_providers())

    def __len__(self) -> int:
        return len(_builtin_providers())

    def __contains__(self, value: object) -> bool:
        return value in _builtin_providers()

    def __eq__(self, other: object) -> bool:
        return _builtin_providers() == other


PROVIDERS = _ProviderNames()


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


async def prompt_text(app, text: str, default: str = "", secret: bool = False) -> str | None:
    if app is not None:
        return await app.ask_text(text, default=default, secret=secret)

    loop = asyncio.get_event_loop()
    if secret:
        import getpass
        result = await loop.run_in_executor(
            None,
            lambda: getpass.getpass(f"  {text}: ").strip(),
        )
        return result if result else default

    result = await loop.run_in_executor(
        None,
        lambda: input(f"  {text}: ").strip(),
    )
    return result if result else default
