"""Slash command registry — single source of truth for dispatch and help."""
from __future__ import annotations

from voidx.presentation.slash import SlashHandler
from voidx.presentation.slash.registry import SLASH_COMMANDS
from voidx.presentation.commands import COMMANDS


def test_registry_entries_are_well_formed() -> None:
    assert SLASH_COMMANDS
    for spec in SLASH_COMMANDS:
        assert spec.name.startswith("/"), spec.name
        assert spec.desc.strip(), spec.name
        assert spec.method, spec.name
        assert callable(getattr(SlashHandler, spec.method)), spec.method
        assert spec.arg in {"none", "args", "inp"}, spec.name


def test_registry_names_are_unique() -> None:
    names = [spec.name for spec in SLASH_COMMANDS]
    assert len(names) == len(set(names))


def test_help_catalog_covers_every_registered_command() -> None:
    catalog_names = {name for name, _desc in COMMANDS}
    registered = {spec.name for spec in SLASH_COMMANDS}
    assert registered <= catalog_names


def test_chat_is_discoverable_in_help_catalog() -> None:
    assert ("/chat", "Start a new chat session") in COMMANDS


def test_catalog_names_are_unique() -> None:
    names = [name for name, _desc in COMMANDS]
    assert len(names) == len(set(names))
