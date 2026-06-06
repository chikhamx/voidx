"""Shared helpers for settings mixins."""

from __future__ import annotations


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
