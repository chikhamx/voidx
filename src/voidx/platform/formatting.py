"""Dependency-neutral formatting helpers."""

from __future__ import annotations


def format_compact_count(value: int | None) -> str:
    count = max(int(value or 0), 0)
    if count >= 1_000_000:
        return _format_scaled(count, 1_000_000, "m")
    if count >= 1_000:
        return _format_scaled(count, 1_000, "k")
    return str(count)


def _format_scaled(value: int, divisor: int, suffix: str) -> str:
    if value % divisor == 0:
        return f"{value // divisor}{suffix}"
    return f"{value / divisor:.1f}{suffix}"
