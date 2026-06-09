"""Helpers for transient assistant placeholder headers."""

from __future__ import annotations

AGENT_PLACEHOLDER_HEADER = "[dim]●[/dim] voidx"
# Compatibility for in-flight or restored trees created before the assistant
# placeholder was renamed from "Working" to "voidx". Keep until pre-rename
# sessions no longer need seamless stream replacement. Remove after 2026-09.
_LEGACY_WORKING_HEADER_PREFIX = "[#EBCB8B]●[/#EBCB8B] Working"


def agent_placeholder_header() -> str:
    return AGENT_PLACEHOLDER_HEADER


def is_agent_placeholder_header(header: str) -> bool:
    return (
        header == AGENT_PLACEHOLDER_HEADER
        or header == _LEGACY_WORKING_HEADER_PREFIX
        or header.startswith(_LEGACY_WORKING_HEADER_PREFIX + " [dim](")
    )
